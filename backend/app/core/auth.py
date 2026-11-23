"""Server-side administrative, organization bearer, and BFF HttpOnly cookie authentication (fail-closed).

Supports:
1. BFF HttpOnly session cookie (`rr_session`) with mandatory CSRF (`X-CSRF-Token`) and
   explicit trusted-Origin validation on state-changing HTTP methods (`POST`, `PUT`, `PATCH`, `DELETE`).
2. Direct `X-Admin-Key` or `Authorization: Bearer <token>` headers for programmatic API clients.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
from typing import Optional

from fastapi import Cookie, Header, HTTPException, Request, status

from app.core.config import get_settings
from app.core.sessions import (
    BffSession,
    SESSION_COOKIE_NAME,
    resolve_bff_session,
    verify_csrf_token,
)
from app.db.database import get_db_connection
from app.schemas.domain import (
    OrganizationBoundaryContext,
    OrganizationRole,
    new_id,
    utc_now_iso,
)

_STATE_CHANGING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def hash_token(raw_token: str) -> str:
    """Return the hex SHA-256 digest of a bearer token."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def issue_org_credential(db_path: str | None, organization_id: str) -> str:
    """Generate a server-side opaque token, persist only its SHA-256 hash, and return the raw token once."""
    raw_token = f"rr_org_{secrets.token_urlsafe(32)}"
    token_hash = hash_token(raw_token)
    with get_db_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO organization_credentials (
                id, organization_id, token_hash, is_active, created_at
            ) VALUES (?, ?, ?, 1, ?)
            """,
            (new_id("cred"), organization_id, token_hash, utc_now_iso()),
        )
    return raw_token


def _resolve_expected_admin_key(request: Request) -> str | None:
    if hasattr(request.app.state, "admin_api_key"):
        return request.app.state.admin_api_key
    return get_settings().admin_api_key


def verify_admin_key_value(request: Request, candidate_key: str | None) -> str:
    expected_key = _resolve_expected_admin_key(request)
    if not expected_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Administrative credential is not configured on the server; write operation denied.",
        )
    if not candidate_key or not hmac.compare_digest(candidate_key.strip(), expected_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Valid administrative credential is required.",
        )
    return "admin"


def get_trusted_origins(request: Request) -> set[str]:
    """Return explicitly configured trusted origins (never blindly trusting Host/X-Forwarded-Host)."""
    app_origins = getattr(request.app.state, "trusted_origins", None)
    if app_origins is not None:
        return {o.strip().rstrip("/") for o in app_origins if o and o.strip()}
    return {o.strip().rstrip("/") for o in get_settings().trusted_origins if o and o.strip()}


def validate_request_origin(request: Request) -> None:
    """If an Origin header is supplied, verify it matches an explicitly trusted application origin."""
    origin = request.headers.get("origin")
    if origin:
        allowed_origins = get_trusted_origins(request)
        if origin.strip().rstrip("/") not in allowed_origins:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Request origin is not permitted for cookie-authenticated mutations.",
            )


def _validate_cookie_csrf_and_origin(
    request: Request, session: BffSession, x_csrf_token: str | None
) -> None:
    if request.method.upper() not in _STATE_CHANGING_METHODS:
        return
    validate_request_origin(request)
    if not verify_csrf_token(session, x_csrf_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing or invalid X-CSRF-Token header for cookie-authenticated mutation.",
        )


def _peer_is_trusted_proxy(peer_ip: str, trusted_entries: list[str]) -> bool:
    if not trusted_entries:
        return True
    try:
        peer_addr = ipaddress.ip_address(peer_ip)
    except ValueError:
        return peer_ip in trusted_entries
    for entry in trusted_entries:
        try:
            if "/" in entry:
                if peer_addr in ipaddress.ip_network(entry, strict=False):
                    return True
            elif peer_addr == ipaddress.ip_address(entry):
                return True
        except ValueError:
            if peer_ip == entry:
                return True
    return False


def resolve_client_ip_for_rate_limit(request: Request) -> str:
    """Resolve client IP strictly from peer address unless TRUST_PROXY_HEADERS is enabled AND peer is trusted."""
    settings = get_settings()
    trust_flag = getattr(request.app.state, "trust_proxy_headers", settings.trust_proxy_headers)
    trusted_proxies = getattr(request.app.state, "trusted_proxy_ips", settings.trusted_proxy_ips)
    peer_ip = request.client.host if (request.client and request.client.host) else "unknown"

    if trust_flag and _peer_is_trusted_proxy(peer_ip, list(trusted_proxies)):
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            # Rightmost untrusted hop prevents client-side X-Forwarded-For spoofing on the left
            hops = [h.strip() for h in forwarded.split(",") if h.strip()]
            for hop in reversed(hops):
                if not trusted_proxies or not _peer_is_trusted_proxy(hop, list(trusted_proxies)):
                    return hop
            if hops:
                return hops[0]
    return peer_ip


def _extract_str_cookie(request: Request, rr_session: Optional[str]) -> str | None:
    if isinstance(rr_session, str):
        return rr_session
    return request.cookies.get(SESSION_COOKIE_NAME)


def _extract_str_csrf(request: Request, x_csrf_token: Optional[str]) -> str | None:
    if isinstance(x_csrf_token, str):
        return x_csrf_token
    return request.headers.get("x-csrf-token")


def _resolve_cookie_session(
    request: Request, rr_session: str | None, x_csrf_token: str | None
) -> BffSession | None:
    cookie_val = _extract_str_cookie(request, rr_session)
    if not cookie_val:
        return None
    csrf_val = _extract_str_csrf(request, x_csrf_token)
    db_path: str | None = getattr(request.app.state, "database_path", None)
    session = resolve_bff_session(db_path, cookie_val)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session is expired or revoked; please sign in again.",
        )
    _validate_cookie_csrf_and_origin(request, session, csrf_val)
    return session


def resolve_bearer_token(
    db_path: str | None, authorization: str
) -> OrganizationBoundaryContext:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header must use Bearer token format.",
        )

    digest = hash_token(token.strip())
    with get_db_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT o.id AS organization_id, o.role AS role, o.data_boundary_id AS data_boundary_id
            FROM organization_credentials c
            JOIN organizations o ON o.id = c.organization_id
            WHERE c.token_hash = ? AND c.is_active = 1
            """,
            (digest,),
        ).fetchone()

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or inactive organization credential.",
        )

    return OrganizationBoundaryContext(
        organization_id=row["organization_id"],
        role=OrganizationRole(row["role"]),
        data_boundary_id=row["data_boundary_id"],
    )


def require_admin_auth(
    request: Request,
    x_admin_key: Optional[str] = Header(default=None, alias="X-Admin-Key"),
    x_csrf_token: Optional[str] = Header(default=None, alias="X-CSRF-Token"),
    rr_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> str:
    """Fail-closed administrative credential check (X-Admin-Key or BFF ADMIN session cookie)."""
    if x_admin_key:
        return verify_admin_key_value(request, x_admin_key)

    session = _resolve_cookie_session(request, rr_session, x_csrf_token)
    if session is not None:
        if session.principal_type != "ADMIN":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Administrator session is required for this operation.",
            )
        return "admin"

    # Trigger 503 if server admin key is unconfigured, otherwise 401
    return verify_admin_key_value(request, None)


def require_org_boundary(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_csrf_token: Optional[str] = Header(default=None, alias="X-CSRF-Token"),
    rr_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> OrganizationBoundaryContext:
    """Fail-closed dependency requiring a valid organization bearer token or BFF ORG session cookie."""
    if authorization:
        db_path: str | None = getattr(request.app.state, "database_path", None)
        return resolve_bearer_token(db_path, authorization)

    session = _resolve_cookie_session(request, rr_session, x_csrf_token)
    if session is not None:
        if (
            session.principal_type != "ORGANIZATION"
            or not session.organization_id
            or not session.organization_role
            or not session.data_boundary_id
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Organization boundary session is required for this operation.",
            )
        return OrganizationBoundaryContext(
            organization_id=session.organization_id,
            role=session.organization_role,
            data_boundary_id=session.data_boundary_id,
        )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Organization credential or active organization session is required.",
    )


def optional_org_boundary(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_csrf_token: Optional[str] = Header(default=None, alias="X-CSRF-Token"),
    rr_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> OrganizationBoundaryContext | None:
    """Resolve organization boundary if credential/cookie is present; fail closed if invalid."""
    if authorization:
        db_path: str | None = getattr(request.app.state, "database_path", None)
        return resolve_bearer_token(db_path, authorization)
    if rr_session:
        session = _resolve_cookie_session(request, rr_session, x_csrf_token)
        if (
            session is not None
            and session.principal_type == "ORGANIZATION"
            and session.organization_id
            and session.organization_role
            and session.data_boundary_id
        ):
            return OrganizationBoundaryContext(
                organization_id=session.organization_id,
                role=session.organization_role,
                data_boundary_id=session.data_boundary_id,
            )
    return None


def require_admin_or_org_auth(
    request: Request,
    x_admin_key: Optional[str] = Header(default=None, alias="X-Admin-Key"),
    authorization: Optional[str] = Header(default=None),
    x_csrf_token: Optional[str] = Header(default=None, alias="X-CSRF-Token"),
    rr_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> tuple[bool, OrganizationBoundaryContext | None]:
    """Allow either valid admin credential/session or valid org credential/session."""
    if x_admin_key:
        verify_admin_key_value(request, x_admin_key)
        return True, None
    if authorization:
        db_path: str | None = getattr(request.app.state, "database_path", None)
        return False, resolve_bearer_token(db_path, authorization)
    if rr_session:
        session = _resolve_cookie_session(request, rr_session, x_csrf_token)
        if session is not None:
            if session.principal_type == "ADMIN":
                return True, None
            if (
                session.principal_type == "ORGANIZATION"
                and session.organization_id
                and session.organization_role
                and session.data_boundary_id
            ):
                return False, OrganizationBoundaryContext(
                    organization_id=session.organization_id,
                    role=session.organization_role,
                    data_boundary_id=session.data_boundary_id,
                )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Administrative credential, organization bearer token, or active session is required.",
    )
