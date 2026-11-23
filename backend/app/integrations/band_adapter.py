"""BAND sponsor adapter using the documented Band Agent REST API (X-API-Key) with two-way shared-room A2A.

Official documentation & REST API references:
- Band API Reference: https://docs.band.ai/api/introduction
- Band Agent REST API: https://docs.band.ai/api/introduction (POST /api/v1/agent/chats/{room_id}/messages)

Exact verified Agent REST endpoints (zero dynamic `getattr` method guessing):
- GET  {base_url}/api/v1/agent/me                         — validate agent credentials (`X-API-Key`)
- POST {base_url}/api/v1/agent/chats/{room_id}/messages   — send text message with `@mentions` routing
- GET  {base_url}/api/v1/agent/chats/{room_id}/context    — read mention-scoped room messages for agent

Protocol invariants:
- Each BAND agent has its own UUID and API key (`BAND_SENDER_AGENT_ID` / `BAND_SENDER_API_KEY`
  and `BAND_RECEIVER_AGENT_ID` / `BAND_RECEIVER_API_KEY`) plus shared `BAND_ROOM_ID`.
- REST authentication sends `X-API-Key` (never `Authorization: Bearer`).
- Mention-based visibility: Band delivers room messages to an agent only when that agent is mentioned.
- Persists verification evidence in SQLite (`sponsor_verification_evidence`) so verified history
  survives server restarts while keeping current configuration/connection state distinct.
- NEVER claims `LIVE_VERIFIED` without an actual successful external operation.
"""

from __future__ import annotations

import importlib.util
import json
import time
from typing import Any, Callable, List

import httpx
from fastapi import HTTPException, status

from app.agents.base import AgentRole
from app.communication.audit_trail import record_audit_event
from app.communication.schemas import A2AMessage, A2AMessageCreate
from app.communication.sharing_service import check_sharing_authorization
from app.communication.transport import InternalSQLiteTransport, MessageTransport
from app.db.database import get_db_connection
from app.integrations.base import (
    BaseSponsorAdapter,
    ConnectionState,
    ImplementationStatus,
    SponsorStatusReport,
    execute_with_bounded_retries,
    get_latest_sponsor_verification_evidence,
    record_integration_usage,
    record_sponsor_verification_evidence,
    redact_secrets,
    validate_sponsor_https_url,
)

DEFAULT_BAND_BASE_URL = "https://app.band.ai"


def is_band_sdk_installed() -> bool:
    """Return True if a BAND client package is installed in this environment."""
    return (
        importlib.util.find_spec("band") is not None
        or importlib.util.find_spec("band_client_rest") is not None
    )


def _extract_band_message_id(data: Any) -> str:
    """Extract the message ID from a documented Band REST response envelope."""
    if isinstance(data, dict):
        nested = data.get("data")
        if isinstance(nested, dict):
            val = nested.get("id") or nested.get("message_id")
            if val:
                return str(val).strip()
        val = data.get("id") or data.get("message_id")
        if val:
            return str(val).strip()
    return ""


def _default_band_rest_send(
    *,
    api_key: str,
    agent_id: str,
    room_id: str,
    content: str,
    mention_agent_id: str | None = None,
    mention_handle: str | None = None,
    rest_url: str | None = None,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Send a room message via Band's documented Agent REST API (`POST /api/v1/agent/chats/{room_id}/messages`)."""
    if not mention_agent_id or not str(mention_agent_id).strip():
        raise ValueError("No mention target is configured for BAND message delivery.")

    base = (rest_url or DEFAULT_BAND_BASE_URL).rstrip("/")
    url = f"{base}/api/v1/agent/chats/{room_id}/messages"

    target_id = str(mention_agent_id).strip()
    handle = mention_handle.strip().lstrip("@") if mention_handle and mention_handle.strip() else None

    headers = {
        "X-API-Key": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    # Attempt with @handle included in message content if handle is configured
    if handle:
        formatted_content = content if content.startswith(f"@{handle}") else f"@{handle} {content}"
        body_with_handle: dict[str, Any] = {
            "message": {
                "content": formatted_content,
                "mentions": [{"id": target_id}],
            }
        }
        try:
            resp = httpx.post(
                url,
                headers=headers,
                json=body_with_handle,
                timeout=timeout_seconds,
                follow_redirects=False,
            )
            if resp.status_code < 400:
                payload = resp.json()
                if not isinstance(payload, dict):
                    raise ValueError("BAND Agent API returned a non-object JSON payload.")
                return payload
            # If rejected with validation error (e.g. 422), fall back to id-only mention
        except (httpx.HTTPStatusError, httpx.RequestError):
            pass

    # Fall back to id-only mention without handle in message content
    body_id_only: dict[str, Any] = {
        "message": {
            "content": content,
            "mentions": [{"id": target_id}],
        }
    }
    resp = httpx.post(
        url,
        headers=headers,
        json=body_id_only,
        timeout=timeout_seconds,
        follow_redirects=False,
    )
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict):
        raise ValueError("BAND Agent API returned a non-object JSON payload.")
    return payload


def _default_band_rest_list_messages(
    *,
    api_key: str,
    agent_id: str,
    room_id: str,
    rest_url: str | None = None,
    timeout_seconds: float = 10.0,
) -> List[dict[str, Any]]:
    """Read mention-scoped room messages via Band's documented Agent REST API (`GET /api/v1/agent/chats/{room_id}/context`)."""
    base = (rest_url or DEFAULT_BAND_BASE_URL).rstrip("/")
    url = f"{base}/api/v1/agent/chats/{room_id}/context"
    resp = httpx.get(
        url,
        headers={
            "X-API-Key": api_key,
            "Accept": "application/json",
        },
        timeout=timeout_seconds,
        follow_redirects=False,
    )
    resp.raise_for_status()
    payload = resp.json()
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        items = payload.get("data") or payload.get("messages") or []
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    return []


class BandMessageTransport(BaseSponsorAdapter, MessageTransport):
    """Band Agent REST API backed MessageTransport supporting one-way and two-way shared-room A2A."""

    sponsor_name = "BAND"
    capability = "Two-way external agent-to-agent shared-room messaging via Band Agent REST API (X-API-Key)"
    official_docs_url = "https://docs.band.ai/api/introduction"
    free_access_summary = (
        "Documented Band Agent REST API (https://docs.band.ai/api/introduction); "
        "requires BAND_SENDER_AGENT_ID, BAND_SENDER_API_KEY, BAND_RECEIVER_AGENT_ID, BAND_RECEIVER_API_KEY, and BAND_ROOM_ID."
    )
    card_or_wallet_required = False
    transport_name = "band_transport"

    def __init__(
        self,
        *,
        db_path: str | None = None,
        api_base_url: str | None = None,
        api_key: str | None = None,
        agent_id: str | None = None,
        sender_handle: str | None = None,
        receiver_api_key: str | None = None,
        receiver_agent_id: str | None = None,
        receiver_handle: str | None = None,
        room_id: str | None = None,
        poll_attempts: int = 10,
        poll_interval_seconds: float = 0.5,
        timeout_seconds: float = 10.0,
        max_retries: int = 2,
        allow_dev_localhost: bool = False,
        sdk_send_fn: Callable[..., dict[str, Any]] | None = None,
        sdk_list_fn: Callable[..., List[dict[str, Any]]] | None = None,
    ) -> None:
        super().__init__(timeout_seconds=timeout_seconds, max_retries=max_retries)
        self.db_path = db_path
        self._local = InternalSQLiteTransport(db_path)
        self._api_key = api_key.strip() if api_key and api_key.strip() else None
        self._agent_id = agent_id.strip() if agent_id and agent_id.strip() else None
        self._sender_handle = (
            sender_handle.strip().lstrip("@") if sender_handle and sender_handle.strip() else None
        )
        self._receiver_api_key = (
            receiver_api_key.strip() if receiver_api_key and receiver_api_key.strip() else None
        )
        self._receiver_agent_id = (
            receiver_agent_id.strip() if receiver_agent_id and receiver_agent_id.strip() else None
        )
        self._receiver_handle = (
            receiver_handle.strip().lstrip("@") if receiver_handle and receiver_handle.strip() else None
        )
        self._room_id = room_id.strip() if room_id and room_id.strip() else None
        self._poll_attempts = max(1, min(int(poll_attempts), 60))
        self._poll_interval_seconds = max(0.0, min(float(poll_interval_seconds), 10.0))
        self._sdk_send_fn = sdk_send_fn
        self._sdk_list_fn = sdk_list_fn
        self._config_error: str | None = None
        self._base_url: str | None = DEFAULT_BAND_BASE_URL
        self._host: str | None = "app.band.ai"
        self._real_message_succeeded: bool = False
        self._two_way_verified: bool = False
        if api_base_url and api_base_url.strip():
            try:
                self._base_url, self._host = validate_sponsor_https_url(
                    api_base_url, allow_dev_localhost=allow_dev_localhost
                )
            except ValueError as exc:
                self._config_error = str(exc)
                self._base_url = None
                self._host = None

    @staticmethod
    def _resolve_handle(agent_id: str | None) -> str | None:
        """Resolve a mention handle from an agent identifier or slug."""
        if not agent_id or not agent_id.strip():
            return None
        cleaned = agent_id.strip().lstrip("@")
        if "/" in cleaned:
            return cleaned.split("/")[-1]
        return cleaned

    def is_configured(self) -> bool:
        """True when primary sender credentials are present and valid."""
        return bool(self._api_key and self._agent_id and not self._config_error)

    def is_two_way_configured(self) -> bool:
        """True only when BOTH distinct sender and receiver agent UUID/API keys + room_id are configured."""
        return bool(
            self.is_configured()
            and self._receiver_api_key
            and self._receiver_agent_id
            and self._receiver_agent_id != self._agent_id
            and self._room_id
        )

    @property
    def real_message_succeeded(self) -> bool:
        return self._real_message_succeeded

    @property
    def two_way_verified(self) -> bool:
        """True ONLY after actual sender -> BAND room -> receiver -> reply -> sender receipt succeeded."""
        return self._two_way_verified

    def _secrets(self) -> List[str | None]:
        return [self._api_key, self._receiver_api_key]

    def _persisted_evidence(self):
        return get_latest_sponsor_verification_evidence(self.db_path, self.sponsor_name)

    def status_report(self) -> SponsorStatusReport:
        persisted = self._persisted_evidence()
        if self._config_error:
            return self._build_report(
                implementation_status=ImplementationStatus.IMPLEMENTED_NOT_CONNECTED,
                connection_state=ConnectionState.ERROR,
                configured=False,
                endpoint_host=None,
                message=f"Invalid BAND configuration: {self._config_error}",
                last_successful_live_operation=persisted,
            )
        if not self.is_configured():
            return self._build_report(
                implementation_status=ImplementationStatus.IMPLEMENTED_NOT_CONNECTED,
                connection_state=ConnectionState.CONFIGURATION_MISSING,
                configured=False,
                endpoint_host=self._host or "app.band.ai",
                message=(
                    "Configure BAND_SENDER_AGENT_ID, BAND_SENDER_API_KEY, BAND_RECEIVER_AGENT_ID, "
                    "BAND_RECEIVER_API_KEY, BAND_ROOM_ID, and A2A_TRANSPORT=band to enable two-way BAND rooms; "
                    "InternalSQLiteTransport remains active."
                ),
                last_successful_live_operation=persisted,
            )
        if self._last_report is not None:
            return self._last_report.model_copy(
                update={"last_successful_live_operation": persisted}
            )
        two_way_note = (
            "Two-way sender+receiver credentials configured."
            if self.is_two_way_configured()
            else "Only sender credentials configured (set BAND_RECEIVER_AGENT_ID, BAND_RECEIVER_API_KEY, and BAND_ROOM_ID for two-way A2A)."
        )
        return self._build_report(
            implementation_status=ImplementationStatus.CONNECTED_UNVERIFIED,
            connection_state=ConnectionState.NOT_CONNECTED,
            configured=True,
            endpoint_host=self._host or "app.band.ai",
            message=f"{two_way_note} Using documented Band Agent REST API directly. No live message verified in this server session yet.",
            last_successful_live_operation=persisted,
        )

    def health_check(
        self, db_path: str | None = None, actor_id: str = "admin"
    ) -> SponsorStatusReport:
        target_db = db_path or self.db_path
        persisted = get_latest_sponsor_verification_evidence(target_db, self.sponsor_name)
        if self._config_error:
            return self._build_report(
                implementation_status=ImplementationStatus.IMPLEMENTED_NOT_CONNECTED,
                connection_state=ConnectionState.ERROR,
                configured=False,
                endpoint_host=None,
                message=f"Invalid BAND configuration: {self._config_error}",
                checked=True,
                last_successful_live_operation=persisted,
            )
        if not self.is_configured():
            return self._build_report(
                implementation_status=ImplementationStatus.IMPLEMENTED_NOT_CONNECTED,
                connection_state=ConnectionState.CONFIGURATION_MISSING,
                configured=False,
                endpoint_host=self._host or "app.band.ai",
                message="BAND sender credentials are not configured.",
                checked=True,
                last_successful_live_operation=persisted,
            )

        # If a test stub is injected, report honest readiness without making a real network call.
        if self._sdk_send_fn is not None:
            impl_status = (
                ImplementationStatus.LIVE_VERIFIED
                if self._real_message_succeeded
                else ImplementationStatus.CONNECTED_UNVERIFIED
            )
            conn_state = (
                ConnectionState.CONNECTED
                if self._real_message_succeeded
                else ConnectionState.NOT_CONNECTED
            )
            return self._build_report(
                implementation_status=impl_status,
                connection_state=conn_state,
                configured=True,
                endpoint_host=self._host or "app.band.ai",
                message=(
                    "BAND two-way room send/receive/reply verified."
                    if self._two_way_verified
                    else (
                        "BAND outbound send verified; run two-way handoff to verify reply."
                        if self._real_message_succeeded
                        else "BAND credentials configured; execute a real A2A send/reply with A2A_TRANSPORT=band to verify."
                    )
                ),
                checked=True,
                last_successful_live_operation=persisted,
            )

        # Live authenticated identity probe against documented GET /api/v1/agent/me (X-API-Key)
        base = (self._base_url or DEFAULT_BAND_BASE_URL).rstrip("/")
        try:
            resp = execute_with_bounded_retries(
                lambda: httpx.get(
                    f"{base}/api/v1/agent/me",
                    headers={"X-API-Key": self._api_key or "", "Accept": "application/json"},
                    timeout=self.timeout_seconds,
                    follow_redirects=False,
                ),
                max_retries=self.max_retries,
            )
            if 200 <= resp.status_code < 300:
                if target_db:
                    record_integration_usage(
                        target_db,
                        sponsor_name=self.sponsor_name,
                        operation="agent_me_health_check",
                        actor_id=actor_id,
                        status="HEALTH_OK",
                    )
                impl_status = (
                    ImplementationStatus.LIVE_VERIFIED
                    if self._real_message_succeeded
                    else ImplementationStatus.CONNECTED_UNVERIFIED
                )
                return self._build_report(
                    implementation_status=impl_status,
                    connection_state=ConnectionState.CONNECTED,
                    configured=True,
                    endpoint_host=self._host or "app.band.ai",
                    message=(
                        f"Band Agent API /api/v1/agent/me reachable (HTTP {resp.status_code}). "
                        + (
                            "Two-way room messaging verified."
                            if self._two_way_verified
                            else "Execute a real A2A room send/reply to verify message delivery."
                        )
                    ),
                    checked=True,
                    last_successful_live_operation=persisted,
                )
            msg = f"Band Agent API /api/v1/agent/me returned HTTP {resp.status_code}."
            if target_db:
                record_integration_usage(
                    target_db,
                    sponsor_name=self.sponsor_name,
                    operation="agent_me_health_check",
                    actor_id=actor_id,
                    status="ERROR",
                    error_summary=msg,
                    secrets_to_mask=self._secrets(),
                )
            return self._build_report(
                implementation_status=ImplementationStatus.CONNECTED_UNVERIFIED,
                connection_state=ConnectionState.ERROR,
                configured=True,
                endpoint_host=self._host or "app.band.ai",
                message=msg,
                checked=True,
                last_successful_live_operation=persisted,
            )
        except httpx.HTTPError as exc:
            err = redact_secrets(f"BAND unreachable: {type(exc).__name__}", self._secrets())
            if target_db:
                record_integration_usage(
                    target_db,
                    sponsor_name=self.sponsor_name,
                    operation="agent_me_health_check",
                    actor_id=actor_id,
                    status="ERROR",
                    error_summary=err,
                    secrets_to_mask=self._secrets(),
                )
            return self._build_report(
                implementation_status=ImplementationStatus.CONNECTED_UNVERIFIED,
                connection_state=ConnectionState.ERROR,
                configured=True,
                endpoint_host=self._host or "app.band.ai",
                message=err,
                checked=True,
                last_successful_live_operation=persisted,
            )

    def _require_configured(self) -> None:
        if not self.is_configured():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="BAND transport is not configured.",
            )

    def _format_room_content(self, payload: A2AMessageCreate, local_msg_id: str, kind: str = "REQUEST") -> str:
        envelope = {
            "protocol": "recallrelay.a2a.v1",
            "kind": kind,
            "local_message_id": local_msg_id,
            "conversation_id": payload.conversation_id,
            "task_id": payload.task_id,
            "recall_id": payload.recall_id,
            "sender_agent": payload.sender_agent.value,
            "recipient_agent": payload.recipient_agent.value,
            "message_type": payload.message_type.value,
            "evidence_references": payload.evidence_references,
            "payload": payload.payload,
        }
        # Keep message content compact: drop None values and omit extra separator whitespace
        compact = {k: v for k, v in envelope.items() if v is not None}
        return json.dumps(compact, separators=(",", ":"), sort_keys=True)

    def _invoke_band_send(
        self,
        *,
        api_key: str,
        agent_id: str,
        room_id: str,
        content: str,
        mention_agent_id: str | None = None,
        mention_handle: str | None = None,
    ) -> dict[str, Any]:
        send_fn = self._sdk_send_fn or _default_band_rest_send
        last_exc: Exception | None = None
        for _ in range(max(1, self.max_retries + 1)):
            try:
                if self._sdk_send_fn is not None:
                    try:
                        return send_fn(
                            api_key=api_key,
                            agent_id=agent_id,
                            room_id=room_id,
                            content=content,
                            mention_agent_id=mention_agent_id,
                            mention_handle=mention_handle,
                            rest_url=self._base_url,
                            timeout_seconds=self.timeout_seconds,
                        )
                    except TypeError:
                        return send_fn(
                            api_key=api_key,
                            agent_id=agent_id,
                            room_id=room_id,
                            content=content,
                            mention_agent_id=mention_agent_id,
                            rest_url=self._base_url,
                            timeout_seconds=self.timeout_seconds,
                        )
                return send_fn(
                    api_key=api_key,
                    agent_id=agent_id,
                    room_id=room_id,
                    content=content,
                    mention_agent_id=mention_agent_id,
                    mention_handle=mention_handle,
                    rest_url=self._base_url,
                    timeout_seconds=self.timeout_seconds,
                )
            except Exception as exc:
                last_exc = exc
        assert last_exc is not None
        raise last_exc

    def _invoke_band_list(
        self, *, api_key: str, agent_id: str, room_id: str
    ) -> List[dict[str, Any]]:
        list_fn = self._sdk_list_fn or _default_band_rest_list_messages
        return list_fn(
            api_key=api_key,
            agent_id=agent_id,
            room_id=room_id,
            rest_url=self._base_url,
            timeout_seconds=self.timeout_seconds,
        )

    def send_message(
        self,
        payload: A2AMessageCreate,
        *,
        sender_boundary_id: str | None,
        actor_id: str,
    ) -> A2AMessage:
        self._require_configured()
        if (
            self.db_path
            and sender_boundary_id
            and payload.recipient_boundary_id
            and sender_boundary_id != payload.recipient_boundary_id
        ):
            allowed, _ = check_sharing_authorization(
                self.db_path,
                recall_id=payload.recall_id,
                owner_boundary_id=sender_boundary_id,
                recipient_boundary_id=payload.recipient_boundary_id,
                requested_fields=sorted(payload.payload.keys()) or None,
            )
            if not allowed:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="External BAND dispatch denied: no active cross-organization sharing grant covers these fields.",
                )
        if not self._receiver_agent_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No mention target is configured for BAND message delivery. Set BAND_RECEIVER_AGENT_ID.",
            )

        local_msg = self._local.send_message(
            payload,
            sender_boundary_id=sender_boundary_id,
            actor_id=actor_id,
        )
        if local_msg.external_message_id and local_msg.transport_provider == "band":
            return local_msg

        room_id = self._room_id or payload.conversation_id
        content = self._format_room_content(payload, local_msg.id, kind="REQUEST")
        receiver_handle = self._receiver_handle or self._resolve_handle(self._receiver_agent_id)
        try:
            band_resp = self._invoke_band_send(
                api_key=self._api_key or "",
                agent_id=self._agent_id or "",
                room_id=room_id,
                content=content,
                mention_agent_id=self._receiver_agent_id,
                mention_handle=receiver_handle,
            )
        except Exception as exc:
            err = redact_secrets(
                f"BAND delivery failed: {type(exc).__name__}: {exc}",
                self._secrets(),
            )
            if self.db_path:
                record_integration_usage(
                    self.db_path,
                    sponsor_name=self.sponsor_name,
                    operation="band_rest_send_message",
                    actor_id=actor_id,
                    boundary_id=sender_boundary_id,
                    status="ERROR",
                    error_summary=err,
                    secrets_to_mask=self._secrets(),
                )
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=err) from exc

        ext_id = _extract_band_message_id(band_resp)
        if not ext_id:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="BAND API response did not include an external message ID.",
            )

        with get_db_connection(self.db_path) as conn:
            conn.execute(
                """
                UPDATE a2a_messages
                SET external_message_id = ?, transport_provider = 'band'
                WHERE id = ?
                """,
                (ext_id, local_msg.id),
            )

        self._real_message_succeeded = True
        persisted = record_sponsor_verification_evidence(
            self.db_path,
            sponsor_name=self.sponsor_name,
            operation_type="band_outbound_message",
            external_reference_id=ext_id,
            sender_identity=self._agent_id,
            receiver_identity=self._receiver_agent_id,
            summary=f"BAND room message delivered (room={room_id}, external_message_id={ext_id}).",
        )
        self._build_report(
            implementation_status=ImplementationStatus.LIVE_VERIFIED,
            connection_state=ConnectionState.CONNECTED,
            configured=True,
            endpoint_host=self._host or "app.band.ai",
            message=f"BAND room message delivered (external_message_id={ext_id}).",
            checked=True,
            last_successful_live_operation=persisted,
        )
        if self.db_path:
            record_integration_usage(
                self.db_path,
                sponsor_name=self.sponsor_name,
                operation="band_rest_send_message",
                actor_id=actor_id,
                boundary_id=sender_boundary_id,
                external_reference_id=ext_id,
                status="DELIVERED",
            )
            record_audit_event(
                self.db_path,
                event_type="A2A_BAND_EXTERNAL_SENT",
                actor_id=actor_id,
                boundary_id=sender_boundary_id,
                entity_type="a2a_message",
                entity_id=local_msg.id,
                metadata={
                    "transport_provider": "band",
                    "external_message_id": ext_id,
                    "room_id": room_id,
                },
            )
        return self._local.get_message(
            local_msg.id, is_admin=True, caller_boundary_id=sender_boundary_id
        )

    def execute_two_way_handoff(
        self,
        payload: A2AMessageCreate,
        *,
        sender_boundary_id: str | None,
        actor_id: str,
        receiver_handler: Callable[[dict[str, Any]], dict[str, Any]],
        poll_attempts: int | None = None,
        poll_interval_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Execute a genuine two-way shared-room A2A round-trip via two distinct BAND agent identities."""
        if not self.is_two_way_configured():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "Two-way BAND communication requires distinct BAND_SENDER_AGENT_ID, "
                    "BAND_SENDER_API_KEY, BAND_RECEIVER_AGENT_ID, BAND_RECEIVER_API_KEY, and BAND_ROOM_ID."
                ),
            )

        effective_attempts = max(1, poll_attempts if poll_attempts is not None else self._poll_attempts)
        effective_interval = (
            max(0.0, poll_interval_seconds)
            if poll_interval_seconds is not None
            else self._poll_interval_seconds
        )

        # Step 1: Sender sends outbound message mentioning receiver
        sent_msg = self.send_message(
            payload, sender_boundary_id=sender_boundary_id, actor_id=actor_id
        )
        if sent_msg.external_reply_message_id:
            return {
                "message": sent_msg,
                "external_message_id": sent_msg.external_message_id,
                "external_reply_message_id": sent_msg.external_reply_message_id,
                "sender_agent_id": self._agent_id,
                "receiver_agent_id": self._receiver_agent_id,
                "replayed": True,
            }

        room_id = self._room_id or payload.conversation_id

        # Step 2: Receiver agent observes inbound message in shared room (configurable bounded poll)
        observed_inbound: dict[str, Any] | None = None
        for attempt in range(effective_attempts):
            room_msgs = self._invoke_band_list(
                api_key=self._receiver_api_key or "",
                agent_id=self._receiver_agent_id or "",
                room_id=room_id,
            )
            for item in room_msgs:
                item_id = _extract_band_message_id(item)
                item_content = str(item.get("content") or "")
                if item_id == sent_msg.external_message_id or sent_msg.id in item_content:
                    observed_inbound = item
                    break
            if observed_inbound is not None:
                break
            if attempt + 1 < effective_attempts and effective_interval > 0:
                time.sleep(effective_interval)

        if observed_inbound is None:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail="BAND receiver agent did not observe the outbound message in the shared room within timeout.",
            )

        # Step 3: Receiver runs genuine agent handler and posts reply mentioning sender
        if not self._agent_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No mention target is configured for BAND reply delivery. Set BAND_SENDER_AGENT_ID.",
            )
        reply_payload = receiver_handler(observed_inbound)
        reply_envelope = {
            "protocol": "recallrelay.a2a.v1",
            "kind": "REPLY",
            "in_reply_to_local_id": sent_msg.id,
            "in_reply_to_external_id": sent_msg.external_message_id,
            "responder_agent": payload.recipient_agent.value,
            "result": reply_payload,
        }
        compact_reply = {k: v for k, v in reply_envelope.items() if v is not None}
        reply_content = json.dumps(
            compact_reply,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        )
        sender_handle = self._sender_handle or self._resolve_handle(self._agent_id)
        try:
            reply_resp = self._invoke_band_send(
                api_key=self._receiver_api_key or "",
                agent_id=self._receiver_agent_id or "",
                room_id=room_id,
                content=reply_content,
                mention_agent_id=self._agent_id,
                mention_handle=sender_handle,
            )
        except Exception as exc:
            err = redact_secrets(
                f"BAND receiver reply failed: {type(exc).__name__}: {exc}",
                self._secrets(),
            )
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=err) from exc

        ext_reply_id = _extract_band_message_id(reply_resp)
        if not ext_reply_id:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="BAND receiver reply did not return an external reply message ID.",
            )

        # Step 4: Sender confirms receipt of the reply in the shared room
        sender_saw_reply = False
        for attempt in range(effective_attempts):
            sender_view = self._invoke_band_list(
                api_key=self._api_key or "",
                agent_id=self._agent_id or "",
                room_id=room_id,
            )
            if any(
                _extract_band_message_id(m) == ext_reply_id
                or ext_reply_id in str(m.get("content") or "")
                for m in sender_view
            ):
                sender_saw_reply = True
                break
            if attempt + 1 < effective_attempts and effective_interval > 0:
                time.sleep(effective_interval)

        if not sender_saw_reply:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail="BAND sender agent did not confirm receipt of the receiver reply within timeout.",
            )

        with get_db_connection(self.db_path) as conn:
            conn.execute(
                """
                UPDATE a2a_messages
                SET external_reply_message_id = ?, external_receiver_agent_id = ?
                WHERE id = ?
                """,
                (ext_reply_id, self._receiver_agent_id, sent_msg.id),
            )

        self._two_way_verified = True
        persisted = record_sponsor_verification_evidence(
            self.db_path,
            sponsor_name=self.sponsor_name,
            operation_type="band_two_way_round_trip",
            external_reference_id=ext_reply_id,
            sender_identity=self._agent_id,
            receiver_identity=self._receiver_agent_id,
            summary=(
                f"Two-way BAND round-trip verified (outbound={sent_msg.external_message_id}, "
                f"reply={ext_reply_id}, room={room_id})."
            ),
        )
        self._build_report(
            implementation_status=ImplementationStatus.LIVE_VERIFIED,
            connection_state=ConnectionState.CONNECTED,
            configured=True,
            endpoint_host=self._host or "app.band.ai",
            message=(
                f"Two-way BAND round-trip verified (outbound={sent_msg.external_message_id}, "
                f"reply={ext_reply_id})."
            ),
            checked=True,
            last_successful_live_operation=persisted,
        )
        if self.db_path:
            record_integration_usage(
                self.db_path,
                sponsor_name=self.sponsor_name,
                operation="band_rest_two_way_reply",
                actor_id=actor_id,
                boundary_id=sender_boundary_id,
                external_reference_id=ext_reply_id,
                status="TWO_WAY_VERIFIED",
            )
            record_audit_event(
                self.db_path,
                event_type="A2A_BAND_TWO_WAY_COMPLETED",
                actor_id=actor_id,
                boundary_id=sender_boundary_id,
                entity_type="a2a_message",
                entity_id=sent_msg.id,
                metadata={
                    "external_message_id": sent_msg.external_message_id,
                    "external_reply_message_id": ext_reply_id,
                    "sender_agent_id": self._agent_id,
                    "receiver_agent_id": self._receiver_agent_id,
                    "room_id": room_id,
                },
            )
        updated_msg = self._local.get_message(
            sent_msg.id, is_admin=True, caller_boundary_id=sender_boundary_id
        )
        return {
            "message": updated_msg,
            "external_message_id": sent_msg.external_message_id,
            "external_reply_message_id": ext_reply_id,
            "sender_agent_id": self._agent_id,
            "receiver_agent_id": self._receiver_agent_id,
            "reply_payload": reply_payload,
            "replayed": False,
        }

    def list_messages(self, **kwargs: Any):
        return self._local.list_messages(**kwargs)

    def get_message(self, message_id: str, **kwargs: Any) -> A2AMessage:
        return self._local.get_message(message_id, **kwargs)

    def deliver_message(self, message_id: str, *, actor_id: str) -> A2AMessage:
        return self._local.deliver_message(message_id, actor_id=actor_id)

    def acknowledge_message(
        self, message_id: str, *, actor_id: str, caller_boundary_id: str | None
    ) -> A2AMessage:
        return self._local.acknowledge_message(
            message_id, actor_id=actor_id, caller_boundary_id=caller_boundary_id
        )

    def claim_next_message(
        self,
        recipient_agent: AgentRole,
        *,
        worker_id: str,
        caller_boundary_id: str | None = None,
    ) -> A2AMessage | None:
        return self._local.claim_next_message(
            recipient_agent,
            worker_id=worker_id,
            caller_boundary_id=caller_boundary_id,
        )

    def process_recipient_inbox(self, *args: Any, **kwargs: Any):
        return self._local.process_recipient_inbox(*args, **kwargs)
