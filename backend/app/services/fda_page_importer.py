"""Administrator-triggered official FDA recall webpage importer (fda.gov allowlist only).

Security & Evidence Provenance Controls:
- HTTPS only, exact hostname allowlist (`www.fda.gov`, `fda.gov`), no userinfo, standard port 443.
- `follow_redirects=False`, explicit timeout, bounded retries, 2 MiB response-size cap.
- Prefers `<main>` / `<article>` content over page chrome (`<nav>`, `<header>`, `<footer>`, `<aside>`).
- Computes and stores BOTH `raw_html_sha256` AND `extracted_text_sha256` (clearly labeled).
- Never truncates main content at 4,000 characters for notes or contradiction analysis.
- Restricts `detect_source_date_contradictions` strictly to sentences discussing product code dates,
  best-before/best-by dates, lot/batch identifiers, or `BB:` date codes — ignoring announcement,
  publish, purchase, and distribution date windows.
- Never misclassifies best-by date codes as batch/lot identifiers: stores date codes as extracted
  product-code claims and reports batch/lot association unresolved when no explicit lot ID is present.
"""

from __future__ import annotations

import hashlib
import re
from html.parser import HTMLParser
from typing import Any, Callable, List
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException, status
from pydantic import BaseModel, Field

from app.agents.base import AgentAuthContext, AgentRole
from app.agents.specialists import build_agent
from app.agents.tools import ToolRegistry
from app.core.config import get_settings
from app.integrations.base import execute_with_bounded_retries
from app.schemas.api_v1 import EvidenceRegisterRequest, EvidenceResponse, EvidenceStage, RecallCreateRequest
from app.schemas.domain import (
    EvidenceSourceType,
    RecallNotice,
    RecallSeverity,
    VerificationStatus,
    utc_now_iso,
)
from app.services import evidence_service, recall_service

_ALLOWED_FDA_HOSTS = {"www.fda.gov", "fda.gov"}
_MAX_HTML_BYTES = 2 * 1024 * 1024  # 2 MiB
_MAX_EXTRACTED_TEXT_CHARS = 100_000

_MONTH_MAP = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

# Positive context requirement for product code-date / best-by / lot sentences
_PRODUCT_CODE_DATE_CONTEXT_RE = re.compile(
    r"\b(?:code\s+dates?|date\s+codes?|best[\s\-]*by|best[\s\-]*before|use[\s\-]*by|"
    r"sell[\s\-]*by|exp(?:iration)?\s*date|bb:|lot\s*(?:number|#|no\.?|code)?|"
    r"batch\s*(?:number|#|no\.?|code)?)\b",
    re.IGNORECASE,
)

# Negative exclusion for announcement/publish/distribution/purchase windows
_NON_CODE_DATE_SENTENCE_RE = re.compile(
    r"(?:company\s+announcement\s+date|fda\s+publish\s+date|content\s+current\s+as\s+of|"
    r"omb\s+approval|distributed\s+(?:between|from|in|nationwide)|"
    r"sold\s+(?:between|from)|purchased\s+(?:between|from)|shipped\s+(?:between|from))",
    re.IGNORECASE,
)


class SourceDateContradiction(BaseModel):
    """Structured contradiction when a single official source states conflicting years for the same code date."""

    month_day: str
    conflicting_values: List[str]
    occurrences: List[dict[str, Any]]
    summary: str


class FdaPageImportRequest(BaseModel):
    url: str = Field(..., min_length=12, max_length=2048)
    create_recall: bool = False
    run_source_analyst: bool = True
    approve_public_disclosure: bool = False


class FdaPageImportResponse(BaseModel):
    source_url: str
    retrieved_at: str
    page_title: str
    issuing_authority: str
    raw_html_sha256: str
    extracted_text_sha256: str
    content_sha256: str
    main_content_extracted: bool = False
    extracted_text_length: int = 0
    publication_date: str | None = None
    termination_notice_detected: bool = False
    extracted_fields: dict[str, Any] = Field(default_factory=dict)
    missing_fields: List[str] = Field(default_factory=list)
    source_date_contradictions: List[SourceDateContradiction] = Field(default_factory=list)
    evidence: EvidenceResponse
    recall: RecallNotice | None = None
    analyst_execution: dict[str, Any] | None = None


class _SafeFdaHtmlParser(HTMLParser):
    """Safe stdlib HTML parser preferring `<main>`/`<article>` content over page chrome."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._chrome_depth = 0
        self._main_depth = 0
        self._in_title = False
        self.title_parts: List[str] = []
        self._current_block: List[str] = []
        self._main_blocks: List[str] = []
        self._body_blocks: List[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
        elif t in {"nav", "header", "footer", "aside"}:
            self._flush_block()
            self._chrome_depth += 1
        elif t in {"main", "article"}:
            self._flush_block()
            self._main_depth += 1
        elif t == "title":
            self._in_title = True
        elif t in {"p", "div", "li", "h1", "h2", "h3", "h4", "tr", "br", "section"}:
            self._flush_block()

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in {"script", "style", "noscript", "svg"} and self._skip_depth > 0:
            self._skip_depth -= 1
        elif t in {"nav", "header", "footer", "aside"} and self._chrome_depth > 0:
            self._flush_block()
            self._chrome_depth -= 1
        elif t in {"main", "article"} and self._main_depth > 0:
            self._flush_block()
            self._main_depth -= 1
        elif t == "title":
            self._in_title = False
        elif t in {"p", "div", "li", "h1", "h2", "h3", "h4", "tr", "section"}:
            self._flush_block()

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        cleaned = re.sub(r"\s+", " ", data).strip()
        if not cleaned:
            return
        if self._in_title:
            self.title_parts.append(cleaned)
        elif self._chrome_depth == 0:
            self._current_block.append(cleaned)

    def _flush_block(self) -> None:
        if self._current_block:
            joined = " ".join(self._current_block).strip()
            if joined:
                self._body_blocks.append(joined)
                if self._main_depth > 0:
                    self._main_blocks.append(joined)
            self._current_block = []

    def get_result(self) -> tuple[str, str, bool]:
        self._flush_block()
        title = " ".join(self.title_parts).strip() or "FDA Safety Announcement"
        if self._main_blocks:
            return title, "\n".join(self._main_blocks), True
        return title, "\n".join(self._body_blocks), False


def validate_official_fda_page_url(raw_url: str) -> str:
    """Strictly validate an official FDA webpage URL (HTTPS, exact fda.gov host, no userinfo, standard port)."""
    cleaned = raw_url.strip()
    parsed = urlparse(cleaned)
    if parsed.scheme != "https":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="FDA webpage import requires an HTTPS URL.",
        )
    if parsed.username or parsed.password or "@" in (parsed.netloc or ""):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="URL userinfo credentials are not permitted.",
        )
    if parsed.port is not None and parsed.port != 443:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only standard HTTPS port 443 is permitted for FDA webpage imports.",
        )
    hostname = (parsed.hostname or "").strip().lower()
    if hostname not in _ALLOWED_FDA_HOSTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Hostname '{hostname}' is not in the official FDA allowlist (www.fda.gov, fda.gov).",
        )
    if not parsed.path or parsed.path == "/":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide a specific official FDA recall announcement page URL, not the bare homepage.",
        )
    return cleaned


def _split_sentences(paragraph: str) -> List[tuple[str, int]]:
    """Split a paragraph into (sentence_text, start_offset_in_paragraph) tuples."""
    results: List[tuple[str, int]] = []
    pos = 0
    for match in re.finditer(r"(?<=[.!?])\s+(?=[A-Z\"'\u201c])", paragraph):
        sent = paragraph[pos : match.start()].strip()
        if sent:
            results.append((sent, pos))
        pos = match.end()
    tail = paragraph[pos:].strip()
    if tail:
        results.append((tail, pos))
    return results


def detect_source_date_contradictions(source_text: str) -> List[SourceDateContradiction]:
    """Detect conflicting years for the same month/day product code date within a single official source.

    Strictly scoped to sentences discussing product code dates, best-before/best-by dates,
    lot/batch identifiers, or `BB:` date codes. Ignores announcement, publish, purchase, and
    distribution date windows. Preserves exact verbatim tokens, paragraph indices, and spans.
    """
    paragraphs = [p.strip() for p in source_text.splitlines() if p.strip()]
    by_md: dict[tuple[int, int], list[dict[str, Any]]] = {}

    numeric_re = re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b")
    named_re = re.compile(
        r"\b(?:BB:\s*)?(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*[\s/\-]+(\d{1,2})[\s/\-,]+(20\d{2})\b",
        re.IGNORECASE,
    )

    for para_idx, para in enumerate(paragraphs, start=1):
        for sentence, sent_offset in _split_sentences(para):
            # Must explicitly discuss product code dates, best-by/best-before, BB:, or lot/batch identifiers
            if not _PRODUCT_CODE_DATE_CONTEXT_RE.search(sentence):
                continue
            # Exclude sentences describing announcement/publish/distribution/purchase windows
            # unless the sentence ALSO explicitly mentions BB: or code dates / date codes
            if _NON_CODE_DATE_SENTENCE_RE.search(sentence) and not re.search(
                r"\b(?:code\s+dates?|date\s+codes?|bb:|best[\s\-]*by|best[\s\-]*before)\b",
                sentence,
                re.IGNORECASE,
            ):
                continue

            for m in numeric_re.finditer(sentence):
                mo, dy, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if 1 <= mo <= 12 and 1 <= dy <= 31:
                    by_md.setdefault((mo, dy), []).append(
                        {
                            "year": yr,
                            "verbatim_value": m.group(0),
                            "paragraph_index": para_idx,
                            "span_start": sent_offset + m.start(),
                            "span_end": sent_offset + m.end(),
                            "excerpt": sentence[max(0, m.start() - 40) : min(len(sentence), m.end() + 40)],
                        }
                    )
            for m in named_re.finditer(sentence):
                mo = _MONTH_MAP.get(m.group(1).lower()[:3], 0)
                dy, yr = int(m.group(2)), int(m.group(3))
                if 1 <= mo <= 12 and 1 <= dy <= 31:
                    by_md.setdefault((mo, dy), []).append(
                        {
                            "year": yr,
                            "verbatim_value": m.group(0),
                            "paragraph_index": para_idx,
                            "span_start": sent_offset + m.start(),
                            "span_end": sent_offset + m.end(),
                            "excerpt": sentence[max(0, m.start() - 40) : min(len(sentence), m.end() + 40)],
                        }
                    )

    contradictions: List[SourceDateContradiction] = []
    for (mo, dy), occs in sorted(by_md.items()):
        years = sorted({o["year"] for o in occs})
        if len(years) > 1:
            vals = sorted({o["verbatim_value"] for o in occs})
            contradictions.append(
                SourceDateContradiction(
                    month_day=f"{mo:02d}/{dy:02d}",
                    conflicting_values=vals,
                    occurrences=occs,
                    summary=(
                        f"Source text contains conflicting years ({', '.join(str(y) for y in years)}) "
                        f"for product code date {mo:02d}/{dy:02d}: {', '.join(vals)}. Both representations preserved verbatim."
                    ),
                )
            )
    return contradictions


_KNOWN_FDA_FIELD_LABELS = {
    "product description",
    "recall reason description",
    "reason for announcement",
    "brand name(s)",
    "brand name",
    "brand names",
    "company name",
    "company announcement date",
    "fda publish date",
    "product type",
    "distribution pattern",
}


def _extract_field_value_skipping_echoes(
    text: str,
    trigger_patterns: list[re.Pattern],
    echo_patterns: list[re.Pattern],
) -> str | None:
    """Extract field value skipping label-echo lines and taking the next non-label line."""
    lines = [line.strip() for line in text.splitlines()]
    for idx, line in enumerate(lines):
        if not line:
            continue
        for trigger in trigger_patterns:
            m = trigger.match(line)
            if not m:
                continue
            # Remainder on the same line after trigger
            remainder = line[m.end():].strip()
            if remainder:
                is_echo = any(echo.match(remainder) for echo in echo_patterns)
                if not is_echo and remainder.lower().rstrip(":") not in _KNOWN_FDA_FIELD_LABELS:
                    return remainder
            # Look ahead across subsequent non-empty lines
            for next_idx in range(idx + 1, len(lines)):
                next_line = lines[next_idx]
                if not next_line:
                    continue
                # Skip lines that echo the label
                if any(echo.match(next_line) for echo in echo_patterns):
                    continue
                # If we encounter another known field label, stop searching
                if next_line.lower().rstrip(":") in _KNOWN_FDA_FIELD_LABELS:
                    break
                return next_line
    return None


def _extract_fda_page_fields(text: str, page_title: str) -> tuple[dict[str, Any], List[str], str | None, bool]:
    """Extract explicitly present fields from cleaned FDA page text without inventing missing values."""
    fields: dict[str, Any] = {}
    missing: List[str] = []

    pub_match = re.search(
        r"(?:FDA Publish Date|Company Announcement Date):\s*([A-Za-z]+\s+\d{1,2},\s+\d{4}|\d{4}-\d{2}-\d{2})",
        text,
        re.IGNORECASE,
    )
    pub_date = pub_match.group(1).strip() if pub_match else None
    if not pub_date:
        pub_candidate = _extract_field_value_skipping_echoes(
            text,
            [re.compile(r"^(?:FDA Publish Date|Company Announcement Date)\s*:?", re.IGNORECASE)],
            [re.compile(r"^(?:FDA Publish Date|Company Announcement Date)\s*:?$", re.IGNORECASE)],
        )
        if pub_candidate:
            date_m = re.search(r"([A-Za-z]+\s+\d{1,2},\s+\d{4}|\d{4}-\d{2}-\d{2})", pub_candidate)
            if date_m:
                pub_date = date_m.group(1).strip()
    if pub_date:
        fields["publication_date"] = pub_date
    else:
        missing.append("publication_date")

    company_name = _extract_field_value_skipping_echoes(
        text,
        [re.compile(r"^Company Name\s*:?", re.IGNORECASE)],
        [re.compile(r"^Company Name\s*:?$", re.IGNORECASE)],
    )
    if company_name:
        fields["company_name"] = company_name
    else:
        missing.append("company_name")

    brand_name = _extract_field_value_skipping_echoes(
        text,
        [re.compile(r"^Brand Name(?:\(s\)|s)?\s*:?", re.IGNORECASE)],
        [re.compile(r"^Brand Name(?:\(s\)|s)?\s*:?$", re.IGNORECASE)],
    )
    if brand_name:
        fields["brand_names"] = brand_name
        fields["brand_name"] = brand_name

    prod_desc = _extract_field_value_skipping_echoes(
        text,
        [re.compile(r"^Product Description\s*:?", re.IGNORECASE)],
        [re.compile(r"^Product Description\s*:?$", re.IGNORECASE)],
    )
    if prod_desc:
        fields["product_description"] = prod_desc
    else:
        fields["product_description"] = page_title
        missing.append("explicit_product_description_label")

    reason = _extract_field_value_skipping_echoes(
        text,
        [re.compile(r"^(?:Recall Reason Description|Reason for Announcement)\s*:?", re.IGNORECASE)],
        [re.compile(r"^(?:Recall Reason Description|Reason for Announcement)\s*:?$", re.IGNORECASE)],
    )
    if reason:
        fields["hazard_or_reason"] = reason
        fields["recall_reason_description"] = reason
    else:
        missing.append("hazard_or_reason")

    # Separate explicit lot/batch IDs from product best-by/code dates (never misclassify date codes as batch numbers)
    explicit_lots = sorted(
        set(
            re.findall(
                r"\b(?:Lot|Batch)\s*(?:Number|No\.?|#)?\s*[:#]?\s*([A-Z0-9][A-Z0-9\-]{2,})\b",
                text,
                re.IGNORECASE,
            )
        )
    )
    if explicit_lots:
        fields["explicit_lot_identifiers"] = explicit_lots
    else:
        fields["batch_association_status"] = "UNRESOLVED_NO_EXPLICIT_LOT_ID"
        missing.append("affected_batch_lot_identifiers")

    bb_codes = sorted(
        set(
            re.findall(
                r"\bBB:\s*[A-Za-z]{3,9}/\d{1,2}/20\d{2}\b",
                text,
                re.IGNORECASE,
            )
        )
    )
    if bb_codes:
        fields["product_code_dates"] = bb_codes

    if not re.search(r"\bClass\s+(?:I|II|III)\b", text):
        missing.append("fda_classification_severity")
    if not re.search(r"\b[FHDZ]-\d{4}-\d{4}\b", text):
        missing.append("fda_enforcement_recall_number")

    missing.extend(["downstream_store_shipments", "downstream_store_inventory"])

    terminated = bool(
        re.search(
            r"This recall has been completed and FDA has terminated this recall",
            text,
            re.IGNORECASE,
        )
    )
    return fields, missing, pub_date, terminated


def import_fda_webpage(
    db_path: str | None,
    payload: FdaPageImportRequest,
    *,
    lyzr_adapter: Any = None,
    http_get: Callable[..., httpx.Response] | None = None,
) -> FdaPageImportResponse:
    """Fetch an official fda.gov recall webpage safely, hash raw HTML + full extracted main text, and register SOURCE_MATERIAL."""
    validated_url = validate_official_fda_page_url(payload.url)
    settings = get_settings()
    getter = http_get or httpx.get
    retrieved_at = utc_now_iso()

    try:
        resp = execute_with_bounded_retries(
            lambda: getter(
                validated_url,
                headers={"Accept": "text/html,application/xhtml+xml", "User-Agent": "RecallRelay/1.0.0"},
                timeout=settings.external_http_timeout_seconds,
                follow_redirects=False,
            ),
            max_retries=settings.external_max_retries,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Unable to fetch official FDA webpage: {type(exc).__name__}.",
        ) from exc

    if 300 <= resp.status_code < 400:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Official FDA URL returned a redirect, which is blocked by security policy.",
        )
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"FDA webpage returned HTTP {resp.status_code}.",
        )

    raw_bytes = resp.content
    if len(raw_bytes) > _MAX_HTML_BYTES:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="FDA webpage exceeded the 2 MiB response-size safety cap.",
        )

    raw_html_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    html_text = raw_bytes.decode("utf-8", errors="replace")
    parser = _SafeFdaHtmlParser()
    parser.feed(html_text)
    page_title, cleaned_text, main_extracted = parser.get_result()
    preserved_text = cleaned_text[:_MAX_EXTRACTED_TEXT_CHARS]
    extracted_text_sha256 = hashlib.sha256(preserved_text.encode("utf-8")).hexdigest()

    extracted_fields, missing_fields, pub_date, terminated = _extract_fda_page_fields(
        preserved_text, page_title
    )
    extracted_fields["raw_html_sha256"] = raw_html_sha256
    extracted_fields["extracted_text_sha256"] = extracted_text_sha256
    contradictions = detect_source_date_contradictions(preserved_text)

    slug = urlparse(validated_url).path.rstrip("/").split("/")[-1]
    record_ref = f"fda-url-slug:{slug}"

    # Store full extracted main content (never truncated at 4,000 chars) + hash provenance header
    provenance_notes = (
        f"[Provenance] raw_html_sha256={raw_html_sha256} | extracted_text_sha256={extracted_text_sha256}\n\n"
        f"{preserved_text}"
    )

    evidence = evidence_service.register_evidence(
        db_path,
        EvidenceRegisterRequest(
            source_type=EvidenceSourceType.OFFICIAL_PUBLIC_NOTICE,
            evidence_stage=EvidenceStage.SOURCE_MATERIAL,
            source_url=validated_url,
            document_content=preserved_text,
            source_organization="U.S. Food and Drug Administration",
            publication_date=pub_date,
            record_reference=record_ref,
            notes=provenance_notes,
        ),
    )

    recall: RecallNotice | None = None
    if payload.create_recall:
        reason_str = extracted_fields.get("hazard_or_reason") or "See official FDA announcement text"
        if terminated:
            reason_str = f"{reason_str} (FDA page states this recall has been completed and terminated)"
        recall = recall_service.create_recall(
            db_path,
            RecallCreateRequest(
                external_recall_number=record_ref,
                title=page_title[:200],
                issuing_authority="U.S. Food and Drug Administration",
                product_description=str(extracted_fields.get("product_description") or page_title)[:300],
                reason=reason_str[:500],
                severity=RecallSeverity.UNKNOWN,
                status=VerificationStatus.AWAITING_EVIDENCE,
                evidence_id=evidence.id,
                published_at=pub_date,
            ),
        )
        if payload.approve_public_disclosure:
            recall = recall_service.set_public_disclosure(
                db_path, recall.id, approved=True, actor_id="admin"
            )

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
            {"evidence_id": evidence.id, "source_text": preserved_text},
            AgentAuthContext(is_admin=True, boundary=None),
        )
        analyst_out = env.model_dump()

    return FdaPageImportResponse(
        source_url=validated_url,
        retrieved_at=retrieved_at,
        page_title=page_title,
        issuing_authority="U.S. Food and Drug Administration",
        raw_html_sha256=raw_html_sha256,
        extracted_text_sha256=extracted_text_sha256,
        content_sha256=extracted_text_sha256,
        main_content_extracted=main_extracted,
        extracted_text_length=len(preserved_text),
        publication_date=pub_date,
        termination_notice_detected=terminated,
        extracted_fields=extracted_fields,
        missing_fields=missing_fields,
        source_date_contradictions=contradictions,
        evidence=evidence,
        recall=recall,
        analyst_execution=analyst_out,
    )
