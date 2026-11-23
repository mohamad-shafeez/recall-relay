"""Administrator-triggered openFDA Food Enforcement public recall importer.

Official API Documentation:
- Endpoint: https://api.fda.gov/food/enforcement.json (keyless public tier: 240 req/min, 1,000 req/day)
- Docs: https://open.fda.gov/apis/food/enforcement/

Security & Real-Data Safeguards:
- Strict destination allowlist: ONLY https://api.fda.gov (never fetches arbitrary user URLs).
- HTTPS enforcement, private/loopback/link-local IP blocking, `follow_redirects=False`.
- Explicit timeout, bounded retries, and 2 MiB response body size limit.
- Computes SHA-256 over the retrieved official JSON payload, preserves source_url and retrieved_at.
- Preserves FDA status honestly as `status_at_publication` and explicitly checks `termination_date`
  (because openFDA does not always update `status` after classification).
- Never fabricates downstream manufacturers, distributors, retailers, shipments, or inventory.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from typing import Any, Callable, List
from urllib.parse import quote, urlencode, urlparse

import httpx
from fastapi import HTTPException, status
from pydantic import BaseModel, Field

from app.agents.base import AgentAuthContext, AgentRole
from app.agents.specialists import build_agent
from app.agents.tools import ToolRegistry
from app.core.config import get_settings
from app.integrations.base import execute_with_bounded_retries, redact_secrets
from app.schemas.api_v1 import BatchCreateRequest, EvidenceRegisterRequest, EvidenceResponse, EvidenceStage, RecallCreateRequest
from app.schemas.domain import (
    AffectedBatch,
    EvidenceRecord,
    RecallNotice,
    RecallSeverity,
    EvidenceSourceType,
    VerificationStatus,
    utc_now_iso,
)
from app.services import evidence_service, recall_service

OPENFDA_FOOD_ENFORCEMENT_URL = "https://api.fda.gov/food/enforcement.json"
_ALLOWED_OPENFDA_HOSTS = {"api.fda.gov"}
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # 2 MiB
_RECALL_NUM_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_]{1,40}$")


class OpenFdaRecallCandidate(BaseModel):
    """Structured preview of an official openFDA food enforcement record."""

    recall_number: str
    event_id: str | None = None
    recalling_firm: str | None = None
    product_description: str | None = None
    reason_for_recall: str | None = None
    classification: str | None = None
    mapped_severity: RecallSeverity = RecallSeverity.UNKNOWN
    status_at_publication: str = "UNKNOWN"
    termination_date: str | None = None
    is_terminated_or_completed: bool = False
    status_caveat: str = (
        "openFDA status reflects status at publication; verify against FDA.gov before treating as live operational state."
    )
    report_date: str | None = None
    recall_initiation_date: str | None = None
    product_quantity: str | None = None
    code_info: str | None = None
    distribution_pattern: str | None = None
    missing_fields: List[str] = Field(default_factory=list)
    source_url: str
    meta_last_updated: str | None = None
    retrieved_at: str


class OpenFdaSearchResponse(BaseModel):
    source_url: str
    meta_last_updated: str | None = None
    retrieved_at: str
    total_matches: int = 0
    results: List[OpenFdaRecallCandidate] = Field(default_factory=list)


class OpenFdaIngestRequest(BaseModel):
    recall_number: str = Field(..., min_length=2, max_length=42)
    run_source_analyst: bool = True
    approve_public_disclosure: bool = False


class OpenFdaIngestResponse(BaseModel):
    source_url: str
    retrieved_at: str
    payload_sha256: str
    status_at_publication: str
    is_terminated_or_completed: bool
    status_caveat: str
    missing_fields: List[str]
    evidence: EvidenceResponse
    recall: RecallNotice
    batches_created: List[AffectedBatch] = Field(default_factory=list)
    analyst_execution: dict[str, Any] | None = None


def _validate_openfda_url(url: str) -> None:
    """Enforce HTTPS, strict api.fda.gov host allowlist, and block private/loopback/link-local IPs."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("openFDA destination must use HTTPS.")
    hostname = (parsed.hostname or "").strip().lower()
    if hostname not in _ALLOWED_OPENFDA_HOSTS:
        raise ValueError(f"Destination host '{hostname}' is not in the official openFDA allowlist.")
    try:
        ip_obj = ipaddress.ip_address(hostname)
        if (
            ip_obj.is_private
            or ip_obj.is_loopback
            or ip_obj.is_link_local
            or ip_obj.is_multicast
            or ip_obj.is_reserved
        ):
            raise ValueError(f"Blocked non-public IP '{hostname}'.")
    except ValueError as exc:
        if "Blocked non-public IP" in str(exc):
            raise


def _format_fda_date(raw: str | None) -> str | None:
    if not raw or not isinstance(raw, str):
        return None
    cleaned = raw.strip()
    if len(cleaned) == 8 and cleaned.isdigit():
        return f"{cleaned[:4]}-{cleaned[4:6]}-{cleaned[6:8]}"
    return cleaned or None


def _map_classification(raw: str | None) -> RecallSeverity:
    cleaned = (raw or "").strip().upper()
    if cleaned == "CLASS I":
        return RecallSeverity.CLASS_I
    if cleaned == "CLASS II":
        return RecallSeverity.CLASS_II
    if cleaned == "CLASS III":
        return RecallSeverity.CLASS_III
    return RecallSeverity.UNKNOWN


def _parse_openfda_candidate(
    raw: dict[str, Any],
    *,
    source_url: str,
    meta_last_updated: str | None,
    retrieved_at: str,
) -> OpenFdaRecallCandidate:
    def _clean(key: str) -> str | None:
        val = raw.get(key)
        if val is None:
            return None
        s = str(val).strip()
        if not s or s.upper() == "N/A":
            return None
        return s

    recall_number = _clean("recall_number") or _clean("event_id") or "UNKNOWN"
    event_id = _clean("event_id")
    recalling_firm = _clean("recalling_firm")
    product_description = _clean("product_description")
    reason_for_recall = _clean("reason_for_recall")
    classification = _clean("classification")
    status_val = _clean("status") or "UNKNOWN"
    termination_date = _format_fda_date(_clean("termination_date"))
    report_date = _format_fda_date(_clean("report_date"))
    initiation_date = _format_fda_date(_clean("recall_initiation_date"))
    product_quantity = _clean("product_quantity")
    code_info = _clean("code_info")
    distribution_pattern = _clean("distribution_pattern")

    is_ended = bool(
        termination_date
        or status_val.lower() in {"terminated", "completed"}
    )
    missing: List[str] = []
    for fname, fval in [
        ("recall_number", _clean("recall_number")),
        ("recalling_firm", recalling_firm),
        ("product_description", product_description),
        ("reason_for_recall", reason_for_recall),
        ("classification", classification),
        ("report_date", report_date),
        ("product_quantity", product_quantity),
        ("code_info", code_info),
        ("distribution_pattern", distribution_pattern),
    ]:
        if not fval:
            missing.append(fname)

    # Always note that openFDA enforcement records never contain private store-level shipment/inventory data
    missing.extend(["downstream_store_shipments", "downstream_store_inventory"])

    return OpenFdaRecallCandidate(
        recall_number=recall_number,
        event_id=event_id,
        recalling_firm=recalling_firm,
        product_description=product_description,
        reason_for_recall=reason_for_recall,
        classification=classification,
        mapped_severity=_map_classification(classification),
        status_at_publication=status_val,
        termination_date=termination_date,
        is_terminated_or_completed=is_ended,
        report_date=report_date,
        recall_initiation_date=initiation_date,
        product_quantity=product_quantity,
        code_info=code_info,
        distribution_pattern=distribution_pattern,
        missing_fields=missing,
        source_url=source_url,
        meta_last_updated=meta_last_updated,
        retrieved_at=retrieved_at,
    )


def _fetch_openfda_json(
    url: str,
    *,
    api_key: str | None = None,
    timeout_seconds: float = 10.0,
    max_retries: int = 2,
    http_get: Callable[..., httpx.Response] | None = None,
) -> tuple[dict[str, Any], bytes, str]:
    """Fetch JSON from allowlisted api.fda.gov with redirect blocking, size cap, and bounded retries."""
    _validate_openfda_url(url)
    request_url = url
    if api_key and "api_key=" not in url:
        sep = "&" if "?" in url else "?"
        request_url = f"{url}{sep}api_key={quote(api_key, safe='')}"

    getter = http_get or httpx.get
    try:
        resp = execute_with_bounded_retries(
            lambda: getter(
                request_url,
                headers={"Accept": "application/json", "User-Agent": "RecallRelay/1.0.0"},
                timeout=timeout_seconds,
                follow_redirects=False,
            ),
            max_retries=max_retries,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=redact_secrets(f"openFDA request failed: {type(exc).__name__}.", [api_key]),
        ) from exc

    if 300 <= resp.status_code < 400:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="openFDA endpoint attempted a redirect, which is blocked by security policy.",
        )
    if resp.status_code == 404:
        return {"meta": {}, "results": []}, b"{}", url
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"openFDA returned HTTP {resp.status_code}.",
        )

    raw_bytes = resp.content
    if len(raw_bytes) > _MAX_RESPONSE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="openFDA response exceeded the 2 MiB safety size cap.",
        )

    try:
        parsed = resp.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="openFDA returned malformed JSON.",
        ) from exc

    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="openFDA payload must be a JSON object.",
        )
    # Return public source_url WITHOUT any api_key query param
    return parsed, raw_bytes, url


def _build_openfda_search_url(
    *,
    status_filter: str | None = None,
    classification: str | None = None,
    query: str | None = None,
    recall_number: str | None = None,
    limit: int = 10,
) -> str:
    clauses: List[str] = []
    if recall_number:
        cleaned_num = recall_number.strip()
        if not _RECALL_NUM_PATTERN.match(cleaned_num):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid openFDA recall_number format.",
            )
        clauses.append(f'recall_number:"{cleaned_num}"')
    else:
        if status_filter and status_filter.strip() in {"Ongoing", "Completed", "Terminated"}:
            clauses.append(f'status:"{status_filter.strip()}"')
        if classification and classification.strip() in {"Class I", "Class II", "Class III"}:
            clauses.append(f'classification:"{classification.strip()}"')
        if query and query.strip():
            safe_q = re.sub(r'[^A-Za-z0-9\s\-]', '', query.strip())[:80]
            if safe_q:
                clauses.append(f'reason_for_recall:"{safe_q}"')

    bounded_limit = max(1, min(limit, 25))
    params: dict[str, str] = {"limit": str(bounded_limit), "sort": "report_date:desc"}
    if clauses:
        # openFDA uses '+' as space inside search clauses
        search_expr = "+AND+".join(c.replace(" ", "+") for c in clauses)
        return f"{OPENFDA_FOOD_ENFORCEMENT_URL}?search={search_expr}&{urlencode(params)}"
    return f"{OPENFDA_FOOD_ENFORCEMENT_URL}?{urlencode(params)}"


def search_openfda_food_recalls(
    *,
    status_filter: str | None = "Ongoing",
    classification: str | None = None,
    query: str | None = None,
    limit: int = 10,
    http_get: Callable[..., httpx.Response] | None = None,
) -> OpenFdaSearchResponse:
    """Query the official openFDA Food Enforcement API and return validated candidate records."""
    settings = get_settings()
    public_url = _build_openfda_search_url(
        status_filter=status_filter,
        classification=classification,
        query=query,
        limit=limit,
    )
    retrieved_at = utc_now_iso()
    data, _, clean_url = _fetch_openfda_json(
        public_url,
        api_key=settings.openfda_api_key,
        timeout_seconds=settings.external_http_timeout_seconds,
        max_retries=settings.external_max_retries,
        http_get=http_get,
    )
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    meta_last_updated = meta.get("last_updated")
    total_matches = (
        int(meta.get("results", {}).get("total", 0))
        if isinstance(meta.get("results"), dict)
        else 0
    )
    raw_results = data.get("results") if isinstance(data.get("results"), list) else []
    candidates = [
        _parse_openfda_candidate(
            item,
            source_url=clean_url,
            meta_last_updated=meta_last_updated,
            retrieved_at=retrieved_at,
        )
        for item in raw_results
        if isinstance(item, dict)
    ]
    return OpenFdaSearchResponse(
        source_url=clean_url,
        meta_last_updated=meta_last_updated,
        retrieved_at=retrieved_at,
        total_matches=total_matches or len(candidates),
        results=candidates,
    )


def ingest_openfda_recall(
    db_path: str | None,
    payload: OpenFdaIngestRequest,
    *,
    lyzr_adapter: Any = None,
    http_get: Callable[..., httpx.Response] | None = None,
) -> OpenFdaIngestResponse:
    """Fetch a specific official openFDA recall record, hash & register it as SOURCE_MATERIAL, and run RecallRelay intake."""
    settings = get_settings()
    public_url = _build_openfda_search_url(recall_number=payload.recall_number, limit=1)
    retrieved_at = utc_now_iso()
    data, _, clean_url = _fetch_openfda_json(
        public_url,
        api_key=settings.openfda_api_key,
        timeout_seconds=settings.external_http_timeout_seconds,
        max_retries=settings.external_max_retries,
        http_get=http_get,
    )
    raw_results = data.get("results") if isinstance(data.get("results"), list) else []
    if not raw_results or not isinstance(raw_results[0], dict):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"openFDA returned no food enforcement record for recall_number '{payload.recall_number}'.",
        )

    record_raw = raw_results[0]
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    candidate = _parse_openfda_candidate(
        record_raw,
        source_url=clean_url,
        meta_last_updated=meta.get("last_updated"),
        retrieved_at=retrieved_at,
    )

    # Canonicalize exact retrieved official payload & compute SHA-256
    canonical_payload = json.dumps(record_raw, sort_keys=True, ensure_ascii=True)
    payload_sha256 = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()

    structured_notes = (
        f"Recall Number: {candidate.recall_number}\n"
        f"Product: {candidate.product_description or 'UNKNOWN'}\n"
        f"Hazard: {candidate.reason_for_recall or 'UNKNOWN'}\n"
        f"Lot: {candidate.code_info or 'UNKNOWN'}\n"
        f"Date: {candidate.report_date or candidate.recall_initiation_date or 'UNKNOWN'}\n"
        f"Status at Publication: {candidate.status_at_publication}"
        + (f" (Termination Date: {candidate.termination_date})" if candidate.termination_date else "")
        + f"\nQuantity: {candidate.product_quantity or 'UNKNOWN'}\n"
        + f"Distribution: {candidate.distribution_pattern or 'UNKNOWN'}\n"
        + f"Retrieved At: {retrieved_at}\n"
        + f"Missing Fields: {', '.join(candidate.missing_fields)}"
    )

    evidence = evidence_service.register_evidence(
        db_path,
        EvidenceRegisterRequest(
            source_type=EvidenceSourceType.OFFICIAL_PUBLIC_NOTICE,
            evidence_stage=EvidenceStage.SOURCE_MATERIAL,
            source_url=clean_url,
            document_content=canonical_payload,
            source_organization="U.S. Food and Drug Administration (openFDA Food Enforcement)",
            publication_date=candidate.report_date or candidate.recall_initiation_date,
            record_reference=candidate.recall_number,
            notes=structured_notes,
        ),
    )

    title_firm = candidate.recalling_firm or "Unknown Firm"
    title_prod = (candidate.product_description or "Food Product")[:120]
    recall = recall_service.create_recall(
        db_path,
        RecallCreateRequest(
            external_recall_number=candidate.recall_number,
            title=f"{title_firm} — {title_prod}",
            issuing_authority="U.S. Food and Drug Administration (openFDA)",
            product_description=candidate.product_description or "UNKNOWN",
            reason=(
                f"{candidate.reason_for_recall or 'UNKNOWN'} "
                f"[openFDA status_at_publication={candidate.status_at_publication}"
                + (
                    f", termination_date={candidate.termination_date}"
                    if candidate.termination_date
                    else ""
                )
                + "]"
            ),
            severity=candidate.mapped_severity,
            status=VerificationStatus.AWAITING_EVIDENCE,
            evidence_id=evidence.id,
            published_at=candidate.report_date or candidate.recall_initiation_date,
        ),
    )

    if payload.approve_public_disclosure:
        recall = recall_service.set_public_disclosure(
            db_path, recall.id, approved=True, actor_id="admin"
        )

    batches_created: List[AffectedBatch] = []
    if candidate.code_info:
        batch = recall_service.add_batch_to_recall(
            db_path,
            recall.id,
            BatchCreateRequest(
                batch_code=candidate.code_info[:120],
                product_name=(candidate.product_description or "UNKNOWN")[:200],
                expiration_or_best_by=None,
                # Never fabricate or guess integer unit counts from free-text product_quantity
                reported_quantity=None,
                quantity_unit=candidate.product_quantity[:80] if candidate.product_quantity else None,
                evidence_id=evidence.id,
            ),
        )
        batches_created.append(batch)

    analyst_out: dict[str, Any] | None = None
    if payload.run_source_analyst:
        agent = build_agent(
            AgentRole.RECALL_SOURCE_ANALYST,
            db_path=db_path,
            tool_registry=ToolRegistry(),
            timeout_seconds=settings.agent_default_timeout_seconds,
            lyzr_adapter=lyzr_adapter,
        )
        env = agent.run(
            {"evidence_id": evidence.id, "source_text": structured_notes},
            AgentAuthContext(is_admin=True, boundary=None),
        )
        analyst_out = env.model_dump()

    return OpenFdaIngestResponse(
        source_url=clean_url,
        retrieved_at=retrieved_at,
        payload_sha256=payload_sha256,
        status_at_publication=candidate.status_at_publication,
        is_terminated_or_completed=candidate.is_terminated_or_completed,
        status_caveat=candidate.status_caveat,
        missing_fields=candidate.missing_fields,
        evidence=evidence,
        recall=recall,
        batches_created=batches_created,
        analyst_execution=analyst_out,
    )
