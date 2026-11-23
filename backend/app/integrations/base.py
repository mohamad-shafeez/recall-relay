"""Centralized sponsor integration contracts, SSRF validation, retries, secret redaction, usage logging, and persistent verification evidence."""

from __future__ import annotations

import ipaddress
import sqlite3
import time
from abc import ABC, abstractmethod
from enum import Enum
from typing import Callable, List, TypeVar
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel

from app.db.database import get_db_connection
from app.schemas.domain import new_id, utc_now_iso

T = TypeVar("T")


class ImplementationStatus(str, Enum):
    """Exact Phase 8/9 integration status vocabulary."""

    LIVE_VERIFIED = "LIVE_VERIFIED"
    IMPLEMENTED_NOT_CONNECTED = "IMPLEMENTED_NOT_CONNECTED"
    CONNECTED_UNVERIFIED = "CONNECTED_UNVERIFIED"
    BLOCKED = "BLOCKED"
    NOT_SUPPORTED = "NOT_SUPPORTED"


class ConnectionState(str, Enum):
    """Runtime connection states shown in the Settings dashboard."""

    CONNECTED = "CONNECTED"
    NOT_CONNECTED = "NOT_CONNECTED"
    CONFIGURATION_MISSING = "CONFIGURATION_MISSING"
    BLOCKED = "BLOCKED"
    DEGRADED = "DEGRADED"
    ERROR = "ERROR"


class SponsorVerificationEvidence(BaseModel):
    """Persisted record of the most recent verified live operation for a sponsor (survives restarts)."""

    id: str
    sponsor_name: str
    operation_type: str
    external_reference_id: str | None = None
    sender_identity: str | None = None
    receiver_identity: str | None = None
    summary: str
    verified_at: str


class SponsorStatusReport(BaseModel):
    """Sanitized status payload for a single sponsor integration (never contains secrets)."""

    sponsor_name: str
    capability: str
    official_docs_url: str
    free_access_summary: str
    card_or_wallet_required: bool
    implementation_status: ImplementationStatus
    connection_state: ConnectionState
    configured: bool
    endpoint_host: str | None = None
    last_checked_at: str | None = None
    last_successful_live_operation: SponsorVerificationEvidence | None = None
    message: str


class IntegrationUsageLog(BaseModel):
    """Sanitized record of an external integration check or invocation."""

    id: str
    sponsor_name: str
    operation: str
    actor_id: str
    boundary_id: str | None
    external_reference_id: str | None
    status: str
    error_summary: str | None
    created_at: str


class IntegrationOverviewResponse(BaseModel):
    """Aggregate status returned by GET /api/v1/integrations/status."""

    configured_a2a_transport: str
    active_a2a_transport: str
    band_real_message_succeeded: bool = False
    lyzr_real_invocation_succeeded: bool = False
    agent_execution_mode: str
    sponsors: List[SponsorStatusReport]


def redact_secrets(text: str, secrets_to_mask: List[str | None]) -> str:
    """Replace any configured secret values with [REDACTED] before logging or returning errors."""
    cleaned = text
    for secret in secrets_to_mask:
        if secret and len(secret) >= 4:
            cleaned = cleaned.replace(secret, "[REDACTED]")
    return cleaned[:500]


def validate_sponsor_https_url(
    raw_url: str | None, *, allow_dev_localhost: bool = False
) -> tuple[str | None, str | None]:
    """Enforce HTTPS and block SSRF targets (private, loopback, link-local, cloud metadata IPs)."""
    if raw_url is None or not raw_url.strip():
        return None, None
    cleaned = raw_url.strip().rstrip("/")
    parsed = urlparse(cleaned)
    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        raise ValueError("Sponsor URL must include a valid hostname.")

    is_localhost = hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https":
        if not (allow_dev_localhost and is_localhost and parsed.scheme == "http"):
            raise ValueError("Sponsor URL must use HTTPS.")

    try:
        ip_obj = ipaddress.ip_address(hostname)
        if not (allow_dev_localhost and ip_obj.is_loopback):
            if (
                ip_obj.is_private
                or ip_obj.is_loopback
                or ip_obj.is_link_local
                or ip_obj.is_multicast
                or ip_obj.is_reserved
                or ip_obj.is_unspecified
            ):
                raise ValueError(f"Blocked SSRF target IP '{hostname}'.")
    except ValueError as exc:
        if "Blocked SSRF target" in str(exc):
            raise
        if hostname in {"localhost", "metadata.google.internal", "169.254.169.254"} and not (
            allow_dev_localhost and hostname == "localhost"
        ):
            raise ValueError(f"Blocked SSRF target host '{hostname}'.")

    return cleaned, hostname


def execute_with_bounded_retries(
    fn: Callable[[], T], *, max_retries: int = 2, backoff_seconds: float = 0.05
) -> T:
    """Run an external HTTP operation with bounded retries on transient transport errors."""
    attempts = max(1, max_retries + 1)
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
            last_exc = exc
            if attempt + 1 < attempts:
                time.sleep(backoff_seconds * (attempt + 1))
    assert last_exc is not None
    raise last_exc


def record_sponsor_verification_evidence(
    db_path: str | None,
    *,
    sponsor_name: str,
    operation_type: str,
    summary: str,
    external_reference_id: str | None = None,
    sender_identity: str | None = None,
    receiver_identity: str | None = None,
) -> SponsorVerificationEvidence | None:
    """Persist durable proof of a real live sponsor operation so LIVE_VERIFIED history survives restarts."""
    if not db_path:
        return None
    ev_id = new_id("spver")
    verified_at = utc_now_iso()
    try:
        with get_db_connection(db_path) as conn:
            conn.execute(
                """
                INSERT INTO sponsor_verification_evidence (
                    id, sponsor_name, operation_type, external_reference_id,
                    sender_identity, receiver_identity, summary, verified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ev_id,
                    sponsor_name,
                    operation_type,
                    external_reference_id,
                    sender_identity,
                    receiver_identity,
                    summary[:500],
                    verified_at,
                ),
            )
    except sqlite3.Error:
        return None
    return SponsorVerificationEvidence(
        id=ev_id,
        sponsor_name=sponsor_name,
        operation_type=operation_type,
        external_reference_id=external_reference_id,
        sender_identity=sender_identity,
        receiver_identity=receiver_identity,
        summary=summary[:500],
        verified_at=verified_at,
    )


def get_latest_sponsor_verification_evidence(
    db_path: str | None, sponsor_name: str
) -> SponsorVerificationEvidence | None:
    """Retrieve the latest persisted live verification evidence for a sponsor, if any."""
    if not db_path:
        return None
    try:
        with get_db_connection(db_path) as conn:
            row = conn.execute(
                """
                SELECT * FROM sponsor_verification_evidence
                WHERE lower(sponsor_name) = lower(?)
                ORDER BY verified_at DESC, id DESC
                LIMIT 1
                """,
                (sponsor_name,),
            ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return SponsorVerificationEvidence(
        id=row["id"],
        sponsor_name=row["sponsor_name"],
        operation_type=row["operation_type"],
        external_reference_id=row["external_reference_id"],
        sender_identity=row["sender_identity"],
        receiver_identity=row["receiver_identity"],
        summary=row["summary"],
        verified_at=row["verified_at"],
    )


def record_integration_usage(
    db_path: str | None,
    *,
    sponsor_name: str,
    operation: str,
    actor_id: str,
    status: str,
    boundary_id: str | None = None,
    external_reference_id: str | None = None,
    error_summary: str | None = None,
    secrets_to_mask: List[str | None] | None = None,
) -> IntegrationUsageLog:
    """Persist a sanitized external integration usage log entry."""
    log_id = new_id("intlog")
    created_at = utc_now_iso()
    safe_error = (
        redact_secrets(error_summary, secrets_to_mask or [])
        if error_summary
        else None
    )
    with get_db_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO external_integration_logs (
                id, sponsor_name, operation, actor_id, boundary_id,
                external_reference_id, status, error_summary, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                log_id,
                sponsor_name,
                operation,
                actor_id,
                boundary_id,
                external_reference_id,
                status,
                safe_error,
                created_at,
            ),
        )
        row = conn.execute(
            "SELECT * FROM external_integration_logs WHERE id = ?", (log_id,)
        ).fetchone()
    return _row_to_log(row)


def _row_to_log(row: sqlite3.Row) -> IntegrationUsageLog:
    return IntegrationUsageLog(
        id=row["id"],
        sponsor_name=row["sponsor_name"],
        operation=row["operation"],
        actor_id=row["actor_id"],
        boundary_id=row["boundary_id"],
        external_reference_id=row["external_reference_id"],
        status=row["status"],
        error_summary=row["error_summary"],
        created_at=row["created_at"],
    )


def list_integration_usage_logs(
    db_path: str | None, *, limit: int = 50
) -> List[IntegrationUsageLog]:
    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM external_integration_logs ORDER BY created_at DESC, id ASC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row_to_log(row) for row in rows]


class BaseSponsorAdapter(ABC):
    """Abstract base class for all five RecallRelay sponsor integrations."""

    sponsor_name: str
    capability: str
    official_docs_url: str
    free_access_summary: str
    card_or_wallet_required: bool = False

    def __init__(self, *, timeout_seconds: float = 10.0, max_retries: int = 2) -> None:
        self.timeout_seconds = min(max(timeout_seconds, 0.5), 30.0)
        self.max_retries = min(max(max_retries, 0), 3)
        self._last_report: SponsorStatusReport | None = None

    @abstractmethod
    def is_configured(self) -> bool:
        """Return True only when required server env vars are present and valid."""

    @abstractmethod
    def status_report(self) -> SponsorStatusReport:
        """Return honest status without fabricating live connectivity."""

    @abstractmethod
    def health_check(self, db_path: str | None = None, actor_id: str = "admin") -> SponsorStatusReport:
        """Execute a real connectivity probe when configured, or return honest unconfigured/blocked state."""

    def _build_report(
        self,
        *,
        implementation_status: ImplementationStatus,
        connection_state: ConnectionState,
        configured: bool,
        endpoint_host: str | None,
        message: str,
        checked: bool = False,
        last_successful_live_operation: SponsorVerificationEvidence | None = None,
    ) -> SponsorStatusReport:
        report = SponsorStatusReport(
            sponsor_name=self.sponsor_name,
            capability=self.capability,
            official_docs_url=self.official_docs_url,
            free_access_summary=self.free_access_summary,
            card_or_wallet_required=self.card_or_wallet_required,
            implementation_status=implementation_status,
            connection_state=connection_state,
            configured=configured,
            endpoint_host=endpoint_host,
            last_checked_at=utc_now_iso() if checked else None,
            last_successful_live_operation=last_successful_live_operation,
            message=message,
        )
        self._last_report = report
        return report
