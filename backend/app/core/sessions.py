"""Server-side BFF session management (HttpOnly cookies, hashed tokens, CSRF, expiration, revocation, cleanup, and login throttling)."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from pydantic import BaseModel

from app.core.config import get_settings
from app.db.database import get_db_connection
from app.schemas.domain import OrganizationRole, new_id, utc_now_iso

SESSION_COOKIE_NAME = "rr_session"


class BffSession(BaseModel):
    """Resolved server-side session record (never contains raw cookie token)."""

    id: str
    principal_type: str  # "ADMIN" | "ORGANIZATION"
    organization_id: str | None = None
    organization_role: OrganizationRole | None = None
    data_boundary_id: str | None = None
    csrf_token: str
    created_at: str
    expires_at: str


def hash_session_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _is_expired(expires_at: str) -> bool:
    try:
        dt = datetime.fromisoformat(expires_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt <= datetime.now(timezone.utc)
    except ValueError:
        return True


def _row_to_session(row: sqlite3.Row) -> BffSession:
    role_val = row["organization_role"]
    return BffSession(
        id=row["id"],
        principal_type=row["principal_type"],
        organization_id=row["organization_id"],
        organization_role=OrganizationRole(role_val) if role_val else None,
        data_boundary_id=row["data_boundary_id"],
        csrf_token=row["csrf_token"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )


def purge_stale_bff_sessions(
    db_path: str | None,
    *,
    max_delete: int = 500,
    revoked_retention_hours: int = 24,
) -> int:
    """Safely purge expired sessions or old revoked sessions without ever touching active sessions."""
    bounded_limit = max(1, min(max_delete, 5000))
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    revoked_cutoff_iso = (now - timedelta(hours=max(0, revoked_retention_hours))).isoformat()

    with get_db_connection(db_path) as conn:
        cur = conn.execute(
            """
            DELETE FROM bff_sessions
            WHERE id IN (
                SELECT id FROM bff_sessions
                WHERE expires_at <= ?
                   OR (revoked_at IS NOT NULL AND revoked_at <= ?)
                ORDER BY expires_at ASC
                LIMIT ?
            )
            """,
            (now_iso, revoked_cutoff_iso, bounded_limit),
        )
        return cur.rowcount


def create_bff_session(
    db_path: str | None,
    *,
    principal_type: str,
    organization_id: str | None = None,
    organization_role: OrganizationRole | None = None,
    data_boundary_id: str | None = None,
    ttl_minutes: int | None = None,
) -> tuple[BffSession, str]:
    """Create a fresh opaque session token, persist only its SHA-256 hash, and return (session, raw_token)."""
    # Opportunistic bounded purge of stale sessions on login without interfering with active sessions
    purge_stale_bff_sessions(db_path, max_delete=100)

    raw_token = f"rr_sess_{secrets.token_urlsafe(32)}"
    token_hash = hash_session_token(raw_token)
    csrf_token = secrets.token_urlsafe(24)
    session_id = new_id("ses")
    now = datetime.now(timezone.utc)
    ttl = ttl_minutes if ttl_minutes is not None else get_settings().session_ttl_minutes
    expires_at = (now + timedelta(minutes=ttl)).isoformat()

    with get_db_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO bff_sessions (
                id, session_token_hash, csrf_token, principal_type,
                organization_id, organization_role, data_boundary_id,
                created_at, expires_at, revoked_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                session_id,
                token_hash,
                csrf_token,
                principal_type,
                organization_id,
                organization_role.value if organization_role else None,
                data_boundary_id,
                now.isoformat(),
                expires_at,
            ),
        )
        row = conn.execute(
            "SELECT * FROM bff_sessions WHERE id = ?", (session_id,)
        ).fetchone()

    return _row_to_session(row), raw_token


def resolve_bff_session(db_path: str | None, raw_token: str | None) -> BffSession | None:
    """Resolve an active, unexpired, unrevoked BFF session from its cookie token."""
    if not raw_token or not raw_token.strip():
        return None
    token_hash = hash_session_token(raw_token.strip())
    with get_db_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM bff_sessions
            WHERE session_token_hash = ? AND revoked_at IS NULL
            """,
            (token_hash,),
        ).fetchone()
    if row is None:
        return None
    if _is_expired(row["expires_at"]):
        return None
    return _row_to_session(row)


def revoke_bff_session_by_token(db_path: str | None, raw_token: str | None) -> bool:
    """Explicitly revoke a session (logout or session rotation to prevent fixation)."""
    if not raw_token or not raw_token.strip():
        return False
    token_hash = hash_session_token(raw_token.strip())
    with get_db_connection(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE bff_sessions
            SET revoked_at = COALESCE(revoked_at, ?)
            WHERE session_token_hash = ?
            """,
            (utc_now_iso(), token_hash),
        )
    return cur.rowcount > 0


def verify_csrf_token(session: BffSession, provided_token: str | None) -> bool:
    if not provided_token:
        return False
    return hmac.compare_digest(session.csrf_token, provided_token.strip())


# ---- Persistent Server-Side Login Throttling ----


def check_login_rate_limit(
    db_path: str | None,
    *,
    scope_key: str,
    max_failures: int | None = None,
    window_seconds: int | None = None,
) -> None:
    """Enforce bounded failed login attempts per scope within a sliding time window."""
    settings = get_settings()
    limit = max_failures if max_failures is not None else settings.login_throttle_max
    window = window_seconds if window_seconds is not None else settings.login_throttle_window_seconds
    lockout = settings.login_throttle_lockout_seconds
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=max(window, lockout))
    cutoff_iso = cutoff.isoformat()

    with get_db_connection(db_path) as conn:
        # Prune stale attempts older than the sliding window
        conn.execute(
            "DELETE FROM login_attempts WHERE attempted_at < ?",
            (cutoff_iso,),
        )
        rows = conn.execute(
            """
            SELECT attempted_at FROM login_attempts
            WHERE scope_key = ? AND success = 0 AND attempted_at >= ?
            ORDER BY attempted_at ASC
            """,
            (scope_key, cutoff_iso),
        ).fetchall()

    if len(rows) >= limit:
        oldest_dt = datetime.fromisoformat(rows[0]["attempted_at"])
        if oldest_dt.tzinfo is None:
            oldest_dt = oldest_dt.replace(tzinfo=timezone.utc)
        retry_after = max(1, int((oldest_dt + timedelta(seconds=lockout) - now).total_seconds()))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed sign-in attempts. Please wait and try again.",
            headers={"Retry-After": str(retry_after)},
        )


def record_login_attempt(
    db_path: str | None, *, scope_key: str, success: bool
) -> None:
    """Record a login attempt; on success, clear prior failure records for this scope."""
    with get_db_connection(db_path) as conn:
        if success:
            conn.execute(
                "DELETE FROM login_attempts WHERE scope_key = ? AND success = 0",
                (scope_key,),
            )
        else:
            conn.execute(
                """
                INSERT INTO login_attempts (id, scope_key, attempted_at, success)
                VALUES (?, ?, ?, 0)
                """,
                (new_id("lgn"), scope_key, utc_now_iso()),
            )
