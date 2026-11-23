"""Lyzr sponsor adapter using verified v3 REST API (docs.lyzr.ai) with boundary-checked verbatim-span grounding.

Verified endpoints:
- GET  https://agent-prod.studio.lyzr.ai/v3/agents/        (x-api-key)
- POST https://agent-prod.studio.lyzr.ai/v3/inference/chat/ (x-api-key)

Every Lyzr response is strictly tagged AI_EXTRACTED / AWAITING_EVIDENCE, checked against
the original source evidence text with word/token boundary enforcement and field-specific
minimum lengths (trivial substrings and unsupported claims are marked UNGROUNDED and excluded),
and can NEVER self-declare VERIFIED.
"""

from __future__ import annotations

import json
import re
from typing import Any, List

import httpx
from fastapi import HTTPException, status

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

_GROUNDABLE_FIELDS = (
    "product_name",
    "lot_number",
    "hazard",
    "publication_date",
    "best_by_date",
    "quantity",
    "distribution_locations",
)

# Field-specific minimum normalized lengths to reject trivial substring matches
_FIELD_MIN_LENGTHS: dict[str, int] = {
    "product_name": 4,
    "lot_number": 3,
    "hazard": 4,
    "publication_date": 4,
    "best_by_date": 4,
    "quantity": 2,
    "distribution_locations": 4,
}


def _normalize_for_span_match(text: str) -> str:
    """Safe documented normalization: collapse whitespace, normalize smart quotes/dashes, lowercase."""
    normalized = (
        text.replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
    )
    return re.sub(r"\s+", " ", normalized).strip().lower()


def _find_boundary_span(norm_source: str, norm_val: str) -> tuple[int, int] | None:
    """Match `norm_val` inside `norm_source` only on alphanumeric token boundaries."""
    if not norm_val:
        return None
    pattern = re.compile(r"(?<![a-z0-9])" + re.escape(norm_val) + r"(?![a-z0-9])")
    match = pattern.search(norm_source)
    if match is None:
        return None
    return match.start(), match.end()


def _parse_candidate_fields_from_lyzr_text(raw_text: str) -> dict[str, str]:
    """Parse structured JSON or `Field: Value` lines from a Lyzr response without inventing fields."""
    stripped = raw_text.strip()
    json_match = re.search(r"\{[\s\S]*\}", stripped)
    if json_match:
        try:
            obj = json.loads(json_match.group(0))
            if isinstance(obj, dict):
                out: dict[str, str] = {}
                for k, v in obj.items():
                    key_norm = str(k).strip().lower().replace(" ", "_")
                    if key_norm in _GROUNDABLE_FIELDS and v is not None and str(v).strip():
                        val = str(v).strip()
                        if val.upper() != "UNKNOWN":
                            out[key_norm] = val
                if out:
                    return out
        except ValueError:
            pass

    label_map = {
        "product": "product_name",
        "product name": "product_name",
        "product_name": "product_name",
        "lot": "lot_number",
        "lot number": "lot_number",
        "lot_number": "lot_number",
        "batch": "lot_number",
        "hazard": "hazard",
        "reason": "hazard",
        "publication date": "publication_date",
        "publication_date": "publication_date",
        "date": "publication_date",
        "best by": "best_by_date",
        "best_by_date": "best_by_date",
        "best-by date": "best_by_date",
        "quantity": "quantity",
        "distribution": "distribution_locations",
        "distribution_locations": "distribution_locations",
    }
    parsed: dict[str, str] = {}
    for line in stripped.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        mapped = label_map.get(k.strip().lstrip("-* ").lower())
        val = v.strip()
        if mapped and val and val.upper() != "UNKNOWN":
            parsed[mapped] = val
    return parsed


def verify_lyzr_grounding(
    *,
    raw_lyzr_text: str,
    source_text: str,
    deterministic_claims: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Verify every Lyzr-extracted field against token-bounded verbatim spans in `source_text`.

    - Rejects trivial or mid-word substrings via alphanumeric word/token boundaries and
      field-specific minimum lengths.
    - Supported claims retain their exact verbatim span offsets and are tagged AI_EXTRACTED / AWAITING_EVIDENCE.
    - Unsupported claims are tagged UNGROUNDED and excluded from grounded_fields.
    - Disagreements with deterministic_claims are preserved explicitly.
    """
    candidates = _parse_candidate_fields_from_lyzr_text(raw_lyzr_text)
    norm_source = _normalize_for_span_match(source_text)

    grounded_fields: dict[str, dict[str, Any]] = {}
    ungrounded_fields: dict[str, dict[str, Any]] = {}
    disagreements: List[dict[str, Any]] = []
    det_map = deterministic_claims or {}

    for field_name, candidate_value in candidates.items():
        norm_val = _normalize_for_span_match(candidate_value)
        min_len = _FIELD_MIN_LENGTHS.get(field_name, 3)
        span = _find_boundary_span(norm_source, norm_val) if len(norm_val) >= min_len else None

        if span is not None:
            start, end = span
            verbatim_quote = norm_source[start:end]
            grounded_fields[field_name] = {
                "candidate_value": candidate_value,
                "grounding_status": "GROUNDED",
                "verbatim_span_normalized": verbatim_quote,
                "span_start": start,
                "span_end": end,
                "evidence_stage": "AI_EXTRACTED",
                "verification_status": "AWAITING_EVIDENCE",
            }
        else:
            reason = (
                f"Candidate value shorter than minimum grounding length ({min_len} chars) for '{field_name}'."
                if len(norm_val) < min_len
                else "Candidate value does not match a token-bounded verbatim span in the authorized source text."
            )
            ungrounded_fields[field_name] = {
                "candidate_value": candidate_value,
                "grounding_status": "UNGROUNDED",
                "excluded_from_recall_record": True,
                "reason": reason,
            }

        det_val = det_map.get(field_name)
        if det_val and _normalize_for_span_match(det_val) != norm_val:
            disagreements.append(
                {
                    "field_name": field_name,
                    "deterministic_value": det_val,
                    "lyzr_candidate_value": candidate_value,
                    "lyzr_grounding_status": (
                        "GROUNDED" if field_name in grounded_fields else "UNGROUNDED"
                    ),
                }
            )

    return {
        "normalization_policy": "lowercase + collapsed whitespace + normalized smart quotes/dashes + alphanumeric token boundaries",
        "field_min_lengths": dict(_FIELD_MIN_LENGTHS),
        "grounded_fields": grounded_fields,
        "ungrounded_fields": ungrounded_fields,
        "ungrounded_count": len(ungrounded_fields),
        "grounded_count": len(grounded_fields),
        "deterministic_disagreements": disagreements,
        "can_self_verify": False,
    }


class LyzrReasoningAdapter(BaseSponsorAdapter):
    """Real HTTPS adapter for Lyzr v3 Agents API wired into RecallSourceAnalystAgent."""

    sponsor_name = "Lyzr"
    capability = "Grounded evidence extraction, contradiction analysis, and recall response summarization (Lyzr v3 Agents API)"
    official_docs_url = "https://docs.lyzr.ai/enterprise/api/agents/chat"
    free_access_summary = "Lyzr Build tier documented as $0 Free forever (lyzr.ai/pricing); requires LYZR_API_KEY and LYZR_AGENT_ID."
    card_or_wallet_required = False

    def __init__(
        self,
        *,
        db_path: str | None = None,
        api_base_url: str = "https://agent-prod.studio.lyzr.ai",
        api_key: str | None = None,
        agent_id: str | None = None,
        timeout_seconds: float = 10.0,
        max_retries: int = 2,
        allow_dev_localhost: bool = False,
    ) -> None:
        super().__init__(timeout_seconds=timeout_seconds, max_retries=max_retries)
        self.db_path = db_path
        self._api_key = api_key.strip() if api_key and api_key.strip() else None
        self._agent_id = agent_id.strip() if agent_id and agent_id.strip() else None
        self._config_error: str | None = None
        self._base_url: str | None = None
        self._host: str | None = None
        self._real_invocation_succeeded: bool = False
        try:
            self._base_url, self._host = validate_sponsor_https_url(
                api_base_url, allow_dev_localhost=allow_dev_localhost
            )
        except ValueError as exc:
            self._config_error = str(exc)

    def is_configured(self) -> bool:
        return bool(self._base_url and self._api_key and not self._config_error)

    def is_reasoning_ready(self) -> bool:
        """True only when both LYZR_API_KEY and LYZR_AGENT_ID are configured."""
        return bool(self.is_configured() and self._agent_id)

    @property
    def real_invocation_succeeded(self) -> bool:
        """True ONLY after an actual /v3/inference/chat/ operation succeeded and returned validated output."""
        return self._real_invocation_succeeded

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
                message=f"Invalid Lyzr URL: {self._config_error}",
                last_successful_live_operation=persisted,
            )
        if not self.is_configured():
            return self._build_report(
                implementation_status=ImplementationStatus.IMPLEMENTED_NOT_CONNECTED,
                connection_state=ConnectionState.CONFIGURATION_MISSING,
                configured=False,
                endpoint_host=self._host,
                message="Configure LYZR_API_KEY and LYZR_AGENT_ID on the server to enable Lyzr v3 reasoning; specialist agents remain DETERMINISTIC.",
                last_successful_live_operation=persisted,
            )
        if self._last_report is not None:
            return self._last_report.model_copy(
                update={"last_successful_live_operation": persisted}
            )
        if not self._agent_id:
            return self._build_report(
                implementation_status=ImplementationStatus.CONNECTED_UNVERIFIED,
                connection_state=ConnectionState.CONFIGURATION_MISSING,
                configured=False,
                endpoint_host=self._host,
                message="LYZR_API_KEY is set, but LYZR_AGENT_ID is missing; Source Analyst stays DETERMINISTIC.",
                last_successful_live_operation=persisted,
            )
        return self._build_report(
            implementation_status=ImplementationStatus.CONNECTED_UNVERIFIED,
            connection_state=ConnectionState.NOT_CONNECTED,
            configured=True,
            endpoint_host=self._host,
            message="LYZR_API_KEY and LYZR_AGENT_ID configured; run Source Analyst or a live check to verify.",
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
                message=f"Invalid Lyzr URL: {self._config_error}",
                checked=True,
                last_successful_live_operation=persisted,
            )
        if not self.is_configured():
            return self._build_report(
                implementation_status=ImplementationStatus.IMPLEMENTED_NOT_CONNECTED,
                connection_state=ConnectionState.CONFIGURATION_MISSING,
                configured=False,
                endpoint_host=self._host,
                message="LYZR_API_KEY is not configured.",
                checked=True,
                last_successful_live_operation=persisted,
            )
        try:
            resp = execute_with_bounded_retries(
                lambda: httpx.get(
                    f"{self._base_url}/v3/agents/",
                    headers={"x-api-key": self._api_key or "", "Accept": "application/json"},
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
                        operation="list_agents_health_check",
                        actor_id=actor_id,
                        status="HEALTH_OK",
                    )
                impl_status = (
                    ImplementationStatus.LIVE_VERIFIED
                    if self._real_invocation_succeeded
                    else ImplementationStatus.CONNECTED_UNVERIFIED
                )
                return self._build_report(
                    implementation_status=impl_status,
                    connection_state=ConnectionState.CONNECTED,
                    configured=self.is_reasoning_ready(),
                    endpoint_host=self._host,
                    message=(
                        f"Lyzr v3 /v3/agents/ reachable (HTTP {resp.status_code}). "
                        + (
                            "Reasoning inference verified."
                            if self._real_invocation_succeeded
                            else "Run Recall Source Analyst to verify live /v3/inference/chat/ reasoning."
                        )
                    ),
                    checked=True,
                    last_successful_live_operation=persisted,
                )
            msg = f"Lyzr v3 /v3/agents/ returned HTTP {resp.status_code}."
            if target_db:
                record_integration_usage(
                    target_db,
                    sponsor_name=self.sponsor_name,
                    operation="list_agents_health_check",
                    actor_id=actor_id,
                    status="ERROR",
                    error_summary=msg,
                    secrets_to_mask=[self._api_key],
                )
            return self._build_report(
                implementation_status=ImplementationStatus.CONNECTED_UNVERIFIED,
                connection_state=ConnectionState.ERROR,
                configured=self.is_reasoning_ready(),
                endpoint_host=self._host,
                message=msg,
                checked=True,
                last_successful_live_operation=persisted,
            )
        except httpx.HTTPError as exc:
            err = redact_secrets(f"Lyzr unreachable: {type(exc).__name__}", [self._api_key])
            if target_db:
                record_integration_usage(
                    target_db,
                    sponsor_name=self.sponsor_name,
                    operation="list_agents_health_check",
                    actor_id=actor_id,
                    status="ERROR",
                    error_summary=err,
                    secrets_to_mask=[self._api_key],
                )
            return self._build_report(
                implementation_status=ImplementationStatus.CONNECTED_UNVERIFIED,
                connection_state=ConnectionState.ERROR,
                configured=self.is_reasoning_ready(),
                endpoint_host=self._host,
                message=err,
                checked=True,
                last_successful_live_operation=persisted,
            )

    def analyze_public_evidence(
        self,
        *,
        user_id: str,
        session_id: str,
        evidence_id: str,
        authorized_text: str,
        actor_id: str = "admin",
        db_path: str | None = None,
        deterministic_claims: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Invoke Lyzr v3 /v3/inference/chat/ on authorized text and run boundary-checked grounding."""
        if not self.is_reasoning_ready():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Lyzr requires LYZR_API_KEY and LYZR_AGENT_ID before executing reasoning.",
            )
        target_db = db_path or self.db_path
        prompt = (
            f"Analyze the following authorized recall evidence ({evidence_id}). "
            "Return JSON with only verbatim values from the text for keys: "
            "product_name, lot_number, hazard, publication_date, best_by_date, quantity, distribution_locations. "
            "Use UNKNOWN for missing fields and NEVER invent facts:\n\n"
            f"{authorized_text[:8000]}"
        )
        try:
            resp = execute_with_bounded_retries(
                lambda: httpx.post(
                    f"{self._base_url}/v3/inference/chat/",
                    headers={
                        "x-api-key": self._api_key or "",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    json={
                        "user_id": user_id,
                        "agent_id": self._agent_id,
                        "session_id": session_id,
                        "message": prompt,
                    },
                    timeout=self.timeout_seconds,
                    follow_redirects=False,
                ),
                max_retries=self.max_retries,
            )
            if resp.status_code >= 400:
                resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                raise ValueError("Lyzr response payload must be a JSON object.")
            raw_text = (
                data.get("response")
                or data.get("message")
                or data.get("output")
            )
            if not isinstance(raw_text, str) or not raw_text.strip():
                raise ValueError("Lyzr response missing non-empty text field ('response').")

            grounding = verify_lyzr_grounding(
                raw_lyzr_text=raw_text.strip(),
                source_text=authorized_text,
                deterministic_claims=deterministic_claims,
            )

            self._real_invocation_succeeded = True
            resolved_session = str(data.get("session_id", session_id))
            persisted = record_sponsor_verification_evidence(
                target_db,
                sponsor_name=self.sponsor_name,
                operation_type="lyzr_v3_inference_chat",
                external_reference_id=resolved_session,
                sender_identity=self._agent_id,
                summary=(
                    f"Lyzr v3 inference succeeded (grounded={grounding['grounded_count']}, "
                    f"ungrounded={grounding['ungrounded_count']})."
                ),
            )
            self._build_report(
                implementation_status=ImplementationStatus.LIVE_VERIFIED,
                connection_state=ConnectionState.CONNECTED,
                configured=True,
                endpoint_host=self._host,
                message="Lyzr v3 /v3/inference/chat/ reasoning verified with token-bounded grounding check.",
                checked=True,
                last_successful_live_operation=persisted,
            )
            if target_db:
                record_integration_usage(
                    target_db,
                    sponsor_name=self.sponsor_name,
                    operation="v3_inference_chat",
                    actor_id=actor_id,
                    external_reference_id=resolved_session,
                    status="COMPLETED",
                )
            return {
                "provider": "Lyzr",
                "agent_id": self._agent_id,
                "session_id": resolved_session,
                "execution_mode": "LYZR_ASSISTED",
                "source_evidence_id": evidence_id,
                "evidence_stage": "AI_EXTRACTED",
                "verification_status": "AWAITING_EVIDENCE",
                "distinguished_from_direct_source": True,
                "can_self_verify": False,
                "model_inference_text": raw_text.strip(),
                "grounding_report": grounding,
            }
        except Exception as exc:
            safe_err = redact_secrets(f"Lyzr inference failed: {type(exc).__name__}", [self._api_key])
            if target_db:
                record_integration_usage(
                    target_db,
                    sponsor_name=self.sponsor_name,
                    operation="v3_inference_chat",
                    actor_id=actor_id,
                    status="ERROR",
                    error_summary=safe_err,
                    secrets_to_mask=[self._api_key],
                )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=safe_err,
            ) from exc
