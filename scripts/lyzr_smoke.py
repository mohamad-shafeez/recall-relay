#!/usr/bin/env python3
"""Standalone live Lyzr v3 verification smoke test (run locally with real .env credentials).

Usage:
    python scripts/lyzr_smoke.py

Verifies:
1. `LYZR_API_KEY` and `LYZR_AGENT_ID` are set.
2. Authenticated `GET /v3/agents/` succeeds (`x-api-key`).
3. Authenticated `POST /v3/inference/chat/` succeeds on a real public FDA excerpt and runs
   token-bounded `verify_lyzr_grounding` (tags output `AI_EXTRACTED` / `AWAITING_EVIDENCE`).

Security: NEVER prints API keys or secrets; exits non-zero on any failure.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import get_settings  # noqa: E402
from app.integrations.base import redact_secrets  # noqa: E402
from app.integrations.lyzr_adapter import LyzrReasoningAdapter  # noqa: E402

SAMPLE_EXCERPT = (
    "Company Announcement Date: November 16, 2026. FDA Publish Date: November 17, 2026. "
    "Company Name: Naia, Inc. Product Description: Chocolate Banana Bar Sorbetto (3oz clear wrapper). "
    "Reason for Announcement: Undeclared Allergen - milk, eggs, tree nut (pistachio). "
    "The recalled product has the date codes BB:Oct/05/2027, BB:Oct/06/2027, BB:Oct/07/2027, or BB:Oct/08/2027."
)


def main() -> int:
    settings = get_settings()
    missing = [
        name
        for name, val in [
            ("LYZR_API_KEY", settings.lyzr_api_key),
            ("LYZR_AGENT_ID", settings.lyzr_agent_id),
        ]
        if not val
    ]
    if missing:
        print(f"FAIL: Missing required environment variables: {', '.join(missing)}")
        return 1

    adapter = LyzrReasoningAdapter(
        db_path=None,
        api_base_url=settings.lyzr_api_base_url,
        api_key=settings.lyzr_api_key,
        agent_id=settings.lyzr_agent_id,
        timeout_seconds=settings.external_http_timeout_seconds,
        max_retries=settings.external_max_retries,
    )
    print(f"Lyzr Smoke Test | base_url={settings.lyzr_api_base_url} | agent_id={settings.lyzr_agent_id}")

    try:
        health = adapter.health_check()
        if health.connection_state.value != "CONNECTED":
            print(f"FAIL: Health check returned {health.connection_state.value}: {health.message}")
            return 2
        print(f"OK: Health check passed ({health.message})")

        result = adapter.analyze_public_evidence(
            user_id="recallrelay_smoke_user",
            session_id="recallrelay_smoke_session",
            evidence_id="smoke_fda_naia_excerpt",
            authorized_text=SAMPLE_EXCERPT,
        )
        grounding = result.get("grounding_report") or {}
        print(
            f"OK: Inference succeeded | evidence_stage={result['evidence_stage']} | "
            f"verification_status={result['verification_status']} | "
            f"grounded={grounding.get('grounded_count', 0)} | ungrounded={grounding.get('ungrounded_count', 0)}"
        )
        print("PASS: Lyzr v3 live smoke verification succeeded.")
        return 0
    except Exception as exc:
        print(f"FAIL: {redact_secrets(f'{type(exc).__name__}: {exc}', [settings.lyzr_api_key])}")
        return 10


if __name__ == "__main__":
    raise SystemExit(main())
