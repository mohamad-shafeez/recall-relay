"""Phase 9 Continuation — Independent Audit Correction Regression Suite.

Covers all confirmed audit findings without fabricating live external connectivity:
1. Lyzr workflow wiring into RecallSourceAnalystAgent (missing config, private org guard,
   validated AI_EXTRACTED / AWAITING_EVIDENCE invocation with deterministic cross-check,
   and honest overview() reporting).
2. BAND Band Agent REST API adapter & explicit A2A_TRANSPORT selection (no auto-switch on
   credentials/health check, external_message_id mapping, failure redaction, sharing grants).
3. Origin & CSRF 6-case security matrix (Vite dev, single-origin FastAPI/Docker, production
   HTTPS, disallowed origin, missing CSRF, invalid CSRF).
4. Administrator-triggered openFDA Food Enforcement public recall importer (allowlist/SSRF/
   redirect/size guards, SHA-256 hashing, status-at-publication & termination_date check,
   missing-field reporting, and verified active Nov 17, 2026 Naia, Inc. FDA recall workflow).
5. Server-side login rate limiting (no X-Forwarded-For bypass) & bounded expired/revoked
   BFF session cleanup.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from app.agents.base import AgentRole
from app.communication.schemas import A2AMessageCreate, A2AMessageType
from app.core.sessions import (
    create_bff_session,
    purge_stale_bff_sessions,
    resolve_bff_session,
    revoke_bff_session_by_token,
)
from app.db.database import get_db_connection
from app.integrations.band_adapter import BandMessageTransport
from app.integrations.base import ImplementationStatus
from app.integrations.lyzr_adapter import LyzrReasoningAdapter
from app.main import create_app
from app.services.openfda_importer import (
    OPENFDA_FOOD_ENFORCEMENT_URL,
    OpenFdaIngestRequest,
    _validate_openfda_url,
    ingest_openfda_recall,
    search_openfda_food_recalls,
)

# Verified active November 17, 2026 official FDA recall notice (no termination banner on FDA.gov)
ACTIVE_FDA_NAIA_URL = (
    "https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts/"
    "naia-inc-recalls-chocolate-banana-bar-sorbetto-because-undeclared-milk-eggs-and-nuts"
)
ACTIVE_FDA_NAIA_EXCERPT = (
    "Title: Naia, Inc. Recalls Chocolate Banana Bar Sorbetto Because of Undeclared Milk, Eggs, and Nuts\n"
    "Company Announcement Date: 2026-11-16\n"
    "FDA Publish Date: 2026-11-17\n"
    "Product: Naia Chocolate Banana Bar Sorbetto (3oz clear wrapper)\n"
    "Lot: BB:Oct/05/2027\n"
    "Hazard: Undeclared Allergen - milk, eggs, tree nut (pistachio)\n"
    "Distribution: retail grocery stores, supermarkets, and other retailers located in Northern California and Reno, NV.\n"
    "Quantity: small but unknown amount."
)


def _mock_httpx_response(
    status_code: int,
    payload: dict | bytes,
    url: str = OPENFDA_FOOD_ENFORCEMENT_URL,
) -> httpx.Response:
    content = (
        payload
        if isinstance(payload, bytes)
        else json.dumps(payload).encode("utf-8")
    )
    return httpx.Response(
        status_code=status_code,
        content=content,
        headers={"content-type": "application/json"},
        request=httpx.Request("GET", url),
    )


class TestPhase9AuditCorrections(unittest.TestCase):
    ADMIN_KEY = "audit-correction-admin-key-2026"

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = str(Path(self._tmpdir.name) / "audit_test.db")
        self.app = create_app(
            database_path=self._db_path,
            admin_api_key=self.ADMIN_KEY,
        )
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _login_admin(self, client: TestClient | None = None, origin: str = "http://localhost:8000") -> dict[str, str]:
        c = client or self.client
        resp = c.post(
            "/api/v1/session/login/admin",
            headers={"Origin": origin},
            json={"admin_key": self.ADMIN_KEY},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return {"X-CSRF-Token": resp.json()["csrf_token"], "Origin": origin}

    # -------------------------------------------------------------------------
    # 1. Lyzr Workflow Wiring & Honest Overview
    # -------------------------------------------------------------------------
    def test_lyzr_missing_config_stays_deterministic_and_honest(self) -> None:
        self.app.state.integration_registry._adapters["lyzr"] = LyzrReasoningAdapter(
            db_path=self._db_path,
            api_key=None,
            agent_id=None,
        )
        headers = self._login_admin()
        ev = self.client.post(
            "/api/v1/evidence",
            headers=headers,
            json={
                "source_type": "OFFICIAL_PUBLIC_NOTICE",
                "evidence_stage": "SOURCE_MATERIAL",
                "source_url": ACTIVE_FDA_NAIA_URL,
                "document_content": ACTIVE_FDA_NAIA_EXCERPT,
                "source_organization": "U.S. Food and Drug Administration",
                "publication_date": "2026-11-17",
                "record_reference": "fda-url-slug:naia-inc-chocolate-banana-bar-sorbetto",
                "notes": ACTIVE_FDA_NAIA_EXCERPT,
            },
        ).json()

        run = self.client.post(
            "/api/v1/agents/RECALL_SOURCE_ANALYST/run",
            headers=headers,
            json={"payload": {"evidence_id": ev["id"]}},
        )
        self.assertEqual(run.status_code, 200, run.text)
        out = run.json()["output"]
        self.assertEqual(out["execution_mode_used"], "DETERMINISTIC")
        self.assertEqual(out["lyzr_status"], "CONFIGURATION_MISSING")
        self.assertIsNone(out["lyzr_analysis"])
        self.assertTrue(out["deterministic_cross_check_preserved"])

        overview = self.client.get("/api/v1/integrations/status", headers=headers).json()
        self.assertEqual(overview["agent_execution_mode"], "DETERMINISTIC")
        self.assertFalse(overview["lyzr_real_invocation_succeeded"])

    def test_lyzr_configured_workflow_wires_into_source_analyst_and_guards_private_evidence(self) -> None:
        # Inject a configured LyzrReasoningAdapter into the registry (unit test mocks HTTP, never claims live network)
        lyzr = LyzrReasoningAdapter(
            db_path=self._db_path,
            api_base_url="https://agent-prod.studio.lyzr.ai",
            api_key="test-lyzr-secret-key",
            agent_id="lyzr_agent_001",
        )
        self.app.state.integration_registry._adapters["lyzr"] = lyzr

        headers = self._login_admin()

        # Health check alone must NOT flip agent_execution_mode to LYZR_ASSISTED
        with patch("app.integrations.lyzr_adapter.httpx.get") as mock_get:
            mock_get.return_value = _mock_httpx_response(200, {"agents": []}, "https://agent-prod.studio.lyzr.ai/v3/agents/")
            hc = self.client.post("/api/v1/integrations/lyzr/check", headers=headers)
            self.assertEqual(hc.status_code, 200)
            self.assertEqual(hc.json()["implementation_status"], "CONNECTED_UNVERIFIED")

        overview_before = self.client.get("/api/v1/integrations/status", headers=headers).json()
        self.assertEqual(overview_before["agent_execution_mode"], "DETERMINISTIC")
        self.assertFalse(overview_before["lyzr_real_invocation_succeeded"])

        # Private AUTHORIZED_ORG_UPLOAD evidence must NOT be sent to Lyzr without explicit authorization
        priv_ev = self.client.post(
            "/api/v1/evidence",
            headers=headers,
            json={
                "source_type": "AUTHORIZED_ORG_UPLOAD",
                "evidence_stage": "SOURCE_MATERIAL",
                "source_document": "private_manifest.csv",
                "document_content": "Lot: PRIV-999\nProduct: Internal Only",
                "source_organization": "Internal Org",
                "record_reference": "priv-manifest-001",
                "notes": "Lot: PRIV-999\nProduct: Internal Only",
            },
        ).json()
        with patch("app.integrations.lyzr_adapter.httpx.post") as mock_post:
            priv_run = self.client.post(
                "/api/v1/agents/RECALL_SOURCE_ANALYST/run",
                headers=headers,
                json={"payload": {"evidence_id": priv_ev["id"]}},
            ).json()["output"]
            self.assertEqual(priv_run["lyzr_status"], "BLOCKED_PRIVATE_EVIDENCE")
            self.assertEqual(priv_run["execution_mode_used"], "DETERMINISTIC")
            mock_post.assert_not_called()

        # Public FDA evidence invokes Lyzr, preserves deterministic cross-check, and stays AI_EXTRACTED / AWAITING_EVIDENCE
        pub_ev = self.client.post(
            "/api/v1/evidence",
            headers=headers,
            json={
                "source_type": "OFFICIAL_PUBLIC_NOTICE",
                "evidence_stage": "SOURCE_MATERIAL",
                "source_url": ACTIVE_FDA_NAIA_URL,
                "document_content": ACTIVE_FDA_NAIA_EXCERPT,
                "source_organization": "U.S. Food and Drug Administration",
                "publication_date": "2026-11-17",
                "record_reference": "fda-url-slug:naia-inc-chocolate-banana-bar-sorbetto",
                "notes": ACTIVE_FDA_NAIA_EXCERPT,
            },
        ).json()

        with patch("app.integrations.lyzr_adapter.httpx.post") as mock_post:
            mock_post.return_value = _mock_httpx_response(
                200,
                {
                    "session_id": f"src_{pub_ev['id']}",
                    "response": "DIRECT SOURCE: Naia Chocolate Banana Bar Sorbetto, undeclared milk/eggs/pistachio, quantity UNKNOWN.",
                },
                "https://agent-prod.studio.lyzr.ai/v3/inference/chat/",
            )
            pub_run = self.client.post(
                "/api/v1/agents/RECALL_SOURCE_ANALYST/run",
                headers=headers,
                json={"payload": {"evidence_id": pub_ev["id"]}},
            ).json()["output"]

        self.assertEqual(pub_run["execution_mode_used"], "DETERMINISTIC_WITH_LYZR_ASSISTED")
        self.assertEqual(pub_run["lyzr_status"], "SUCCEEDED")
        self.assertTrue(pub_run["deterministic_cross_check_preserved"])
        self.assertEqual(pub_run["evidence_verification_status"], "AWAITING_EVIDENCE")
        self.assertEqual(pub_run["lyzr_analysis"]["evidence_stage"], "AI_EXTRACTED")
        self.assertEqual(pub_run["lyzr_analysis"]["verification_status"], "AWAITING_EVIDENCE")
        self.assertFalse(pub_run["lyzr_analysis"]["can_self_verify"])

        overview_after = self.client.get("/api/v1/integrations/status", headers=headers).json()
        self.assertEqual(overview_after["agent_execution_mode"], "DETERMINISTIC + LYZR_ASSISTED")
        self.assertTrue(overview_after["lyzr_real_invocation_succeeded"])

    # -------------------------------------------------------------------------
    # 2. BAND Official SDK Adapter & Explicit A2A_TRANSPORT Selection
    # -------------------------------------------------------------------------
    def test_band_explicit_transport_selection_and_external_message_id_mapping(self) -> None:
        # Create a recall anchored to real evidence
        headers = self._login_admin()
        ev = self.client.post(
            "/api/v1/evidence",
            headers=headers,
            json={
                "source_type": "OFFICIAL_PUBLIC_NOTICE",
                "evidence_stage": "SOURCE_MATERIAL",
                "source_url": ACTIVE_FDA_NAIA_URL,
                "document_content": ACTIVE_FDA_NAIA_EXCERPT,
                "source_organization": "U.S. Food and Drug Administration",
                "publication_date": "2026-11-17",
                "record_reference": "fda-naia-band-001",
                "notes": ACTIVE_FDA_NAIA_EXCERPT,
            },
        ).json()
        recall = self.client.post(
            "/api/v1/recalls",
            headers=headers,
            json={
                "external_recall_number": "fda-url-slug:naia-inc-2026-11-17",
                "title": "Naia, Inc. Chocolate Banana Bar Sorbetto Allergy Alert",
                "issuing_authority": "U.S. Food and Drug Administration",
                "product_description": "Naia Chocolate Banana Bar Sorbetto, 3oz clear wrapper",
                "reason": "Undeclared Allergen - milk, eggs, tree nut (pistachio)",
                "severity": "UNKNOWN",
                "status": "AWAITING_EVIDENCE",
                "evidence_id": ev["id"],
                "published_at": "2026-11-17",
            },
        ).json()

        # Even if BAND credentials exist, A2A_TRANSPORT=internal_sqlite must NOT auto-switch to BAND
        captured_calls: list[dict] = []

        def fake_sdk_send(**kwargs):
            captured_calls.append(kwargs)
            return {"id": "band_msg_ext_98765", "room_id": kwargs["room_id"]}

        band_adapter = BandMessageTransport(
            db_path=self._db_path,
            api_key="band-secret-key-1234",
            agent_id="11111111-2222-3333-4444-555555555555",
            receiver_agent_id="22222222-3333-4444-5555-666666666666",
            room_id="room_recall_001",
            sdk_send_fn=fake_sdk_send,
        )
        self.app.state.integration_registry._adapters["band"] = band_adapter

        ov_default = self.client.get("/api/v1/integrations/status", headers=headers).json()
        self.assertEqual(ov_default["configured_a2a_transport"], "internal_sqlite")
        self.assertEqual(ov_default["active_a2a_transport"], "internal_sqlite_transport")
        self.assertFalse(ov_default["band_real_message_succeeded"])

        # Health check alone does NOT claim LIVE_VERIFIED
        hc = self.client.post("/api/v1/integrations/band/check", headers=headers).json()
        self.assertEqual(hc["implementation_status"], "CONNECTED_UNVERIFIED")

        # Now explicitly select A2A_TRANSPORT=band
        self.app.state.integration_registry.configured_a2a_transport = "band"
        ov_band_before = self.client.get("/api/v1/integrations/status", headers=headers).json()
        self.assertEqual(ov_band_before["active_a2a_transport"], "band_transport")
        self.assertFalse(ov_band_before["band_real_message_succeeded"])

        # Send a message from Recall Scope Agent -> Independent Verification Agent via /api/v1/a2a/messages
        msg_resp = self.client.post(
            "/api/v1/a2a/messages",
            headers=headers,
            json={
                "conversation_id": "conv_naia_band_001",
                "recall_id": recall["id"],
                "sender_agent": "RECALL_SCOPE_AGENT",
                "recipient_agent": "INDEPENDENT_VERIFICATION_AGENT",
                "message_type": "TASK_HANDOFF",
                "payload": {"recall_id": recall["id"]},
                "evidence_references": [ev["id"]],
                "idempotency_key": "band-send-key-001",
            },
        )
        self.assertEqual(msg_resp.status_code, 201, msg_resp.text)
        msg_data = msg_resp.json()
        self.assertEqual(msg_data["transport_provider"], "band")
        self.assertEqual(msg_data["external_message_id"], "band_msg_ext_98765")
        self.assertEqual(len(captured_calls), 1)
        self.assertEqual(captured_calls[0]["room_id"], "room_recall_001")

        # Only now does overview report band_real_message_succeeded=True and LIVE_VERIFIED
        ov_band_after = self.client.get("/api/v1/integrations/status", headers=headers).json()
        self.assertTrue(ov_band_after["band_real_message_succeeded"])
        band_status = next(s for s in ov_band_after["sponsors"] if s["sponsor_name"] == "BAND")
        self.assertEqual(band_status["implementation_status"], ImplementationStatus.LIVE_VERIFIED.value)

    def test_band_failure_redacts_secret_and_never_claims_verified(self) -> None:
        def failing_sdk_send(**kwargs):
            raise RuntimeError("Upstream error for key band-super-secret-999")

        band_adapter = BandMessageTransport(
            db_path=self._db_path,
            api_key="band-super-secret-999",
            agent_id="11111111-2222-3333-4444-555555555555",
            receiver_agent_id="22222222-3333-4444-5555-666666666666",
            room_id="room_recall_001",
            max_retries=0,
            sdk_send_fn=failing_sdk_send,
        )
        self.app.state.integration_registry._adapters["band"] = band_adapter
        self.app.state.integration_registry.configured_a2a_transport = "band"

        headers = self._login_admin()
        ev = self.client.post(
            "/api/v1/evidence",
            headers=headers,
            json={
                "source_type": "OFFICIAL_PUBLIC_NOTICE",
                "evidence_stage": "SOURCE_MATERIAL",
                "source_url": ACTIVE_FDA_NAIA_URL,
                "document_content": ACTIVE_FDA_NAIA_EXCERPT,
                "source_organization": "FDA",
                "record_reference": "fda-ref-test",
            },
        ).json()
        recall = self.client.post(
            "/api/v1/recalls",
            headers=headers,
            json={
                "external_recall_number": "fda-url-slug:naia-fail-test",
                "title": "Naia Failure Test",
                "issuing_authority": "FDA",
                "product_description": "Sorbetto",
                "reason": "Undeclared milk",
                "severity": "UNKNOWN",
                "evidence_id": ev["id"],
            },
        ).json()

        resp = self.client.post(
            "/api/v1/a2a/messages",
            headers=headers,
            json={
                "conversation_id": "conv_fail",
                "recall_id": recall["id"],
                "sender_agent": "RECALL_SCOPE_AGENT",
                "recipient_agent": "INDEPENDENT_VERIFICATION_AGENT",
                "message_type": "TASK_HANDOFF",
                "payload": {},
            },
        )
        self.assertEqual(resp.status_code, 502)
        self.assertNotIn("band-super-secret-999", resp.text)
        self.assertIn("[REDACTED]", resp.text)
        self.assertFalse(band_adapter.real_message_succeeded)

    # -------------------------------------------------------------------------
    # 3. Origin & CSRF 6-Case Regression Suite
    # -------------------------------------------------------------------------
    def test_origin_and_csrf_six_case_matrix(self) -> None:
        prod_app = create_app(
            database_path=str(Path(self._tmpdir.name) / "origin_test.db"),
            admin_api_key=self.ADMIN_KEY,
            trusted_origins=[
                "http://localhost:5173",
                "http://localhost:8000",
                "https://recallrelay.example.com",
            ],
        )
        client = TestClient(prod_app)

        # Case 1: Vite development origin -> allowed
        vite_headers = self._login_admin(client, origin="http://localhost:5173")
        r1 = client.post(
            "/api/v1/evidence",
            headers=vite_headers,
            json={
                "source_type": "OFFICIAL_PUBLIC_NOTICE",
                "evidence_stage": "SOURCE_MATERIAL",
                "source_url": ACTIVE_FDA_NAIA_URL,
                "document_content": "Case 1 Vite dev",
                "source_organization": "FDA",
                "record_reference": "fda-ref-test",
            },
        )
        self.assertEqual(r1.status_code, 201, r1.text)

        # Case 2: Single-origin FastAPI/Docker frontend (http://localhost:8000) -> allowed
        single_headers = self._login_admin(client, origin="http://localhost:8000")
        r2 = client.post(
            "/api/v1/evidence",
            headers=single_headers,
            json={
                "source_type": "OFFICIAL_PUBLIC_NOTICE",
                "evidence_stage": "SOURCE_MATERIAL",
                "source_url": ACTIVE_FDA_NAIA_URL,
                "document_content": "Case 2 Single-origin FastAPI",
                "source_organization": "FDA",
                "record_reference": "fda-ref-test",
            },
        )
        self.assertEqual(r2.status_code, 201, r2.text)

        # Case 3: Configured production HTTPS origin -> allowed
        prod_headers = self._login_admin(client, origin="https://recallrelay.example.com")
        r3 = client.post(
            "/api/v1/evidence",
            headers=prod_headers,
            json={
                "source_type": "OFFICIAL_PUBLIC_NOTICE",
                "evidence_stage": "SOURCE_MATERIAL",
                "source_url": ACTIVE_FDA_NAIA_URL,
                "document_content": "Case 3 Prod HTTPS",
                "source_organization": "FDA",
                "record_reference": "fda-ref-test",
            },
        )
        self.assertEqual(r3.status_code, 201, r3.text)

        # Case 4: Disallowed origin -> 403 (even if Host header is spoofed)
        r4 = client.post(
            "/api/v1/evidence",
            headers={
                "X-CSRF-Token": prod_headers["X-CSRF-Token"],
                "Origin": "https://attacker.evil.example",
                "Host": "attacker.evil.example",
            },
            json={
                "source_type": "OFFICIAL_PUBLIC_NOTICE",
                "evidence_stage": "SOURCE_MATERIAL",
                "source_url": ACTIVE_FDA_NAIA_URL,
                "document_content": "Case 4 Disallowed",
                "source_organization": "FDA",
                "record_reference": "fda-ref-test",
            },
        )
        self.assertEqual(r4.status_code, 403)

        # Case 5: Missing CSRF token -> 403
        r5 = client.post(
            "/api/v1/evidence",
            headers={"Origin": "http://localhost:8000"},
            json={
                "source_type": "OFFICIAL_PUBLIC_NOTICE",
                "evidence_stage": "SOURCE_MATERIAL",
                "source_url": ACTIVE_FDA_NAIA_URL,
                "document_content": "Case 5 Missing CSRF",
                "source_organization": "FDA",
                "record_reference": "fda-ref-test",
            },
        )
        self.assertEqual(r5.status_code, 403)

        # Case 6: Invalid CSRF token -> 403
        r6 = client.post(
            "/api/v1/evidence",
            headers={
                "Origin": "http://localhost:8000",
                "X-CSRF-Token": "invalid-csrf-token-value",
            },
            json={
                "source_type": "OFFICIAL_PUBLIC_NOTICE",
                "evidence_stage": "SOURCE_MATERIAL",
                "source_url": ACTIVE_FDA_NAIA_URL,
                "document_content": "Case 6 Invalid CSRF",
                "source_organization": "FDA",
                "record_reference": "fda-ref-test",
            },
        )
        self.assertEqual(r6.status_code, 403)

    # -------------------------------------------------------------------------
    # 4. openFDA Public Recall Importer & Security Guards
    # -------------------------------------------------------------------------
    def test_openfda_importer_security_guards_and_ingestion(self) -> None:
        # Security: block non-allowlisted hosts and non-HTTPS schemes
        with self.assertRaises(ValueError):
            _validate_openfda_url("http://api.fda.gov/food/enforcement.json")
        with self.assertRaises(ValueError):
            _validate_openfda_url("https://169.254.169.254/latest/meta-data/")
        with self.assertRaises(ValueError):
            _validate_openfda_url("https://evil.example.com/food/enforcement.json")

        # Schema-faithful openFDA Food Enforcement payload (unit test uses local HTTP stub, not live network)
        sample_record = {
            "recall_number": "F-0412-2026",
            "event_id": "99104",
            "status": "Ongoing",
            "classification": "Class I",
            "recalling_firm": "Naia, Inc.",
            "product_description": "Naia Chocolate Banana Bar Sorbetto, 3oz clear wrapper",
            "reason_for_recall": "Undeclared milk, eggs, and tree nut (pistachio)",
            "report_date": "20261118",
            "recall_initiation_date": "20261116",
            "code_info": "BB:Oct/05/2027, BB:Oct/06/2027, BB:Oct/07/2027, BB:Oct/08/2027",
            "distribution_pattern": "Northern California and Reno, NV",
            # product_quantity deliberately omitted to verify missing-field reporting
        }
        terminated_quirk_record = {
            "recall_number": "F-2287-2017",
            "status": "Ongoing",  # openFDA documented quirk: status still Ongoing despite termination_date
            "termination_date": "20170803",
            "classification": "Class II",
            "recalling_firm": "Historical Example",
            "product_description": "Historical Product",
            "reason_for_recall": "Labeling issue",
        }
        api_payload = {
            "meta": {"last_updated": "2026-11-18", "results": {"total": 2}},
            "results": [sample_record, terminated_quirk_record],
        }

        search_res = search_openfda_food_recalls(
            status_filter="Ongoing",
            limit=5,
            http_get=lambda *a, **kw: _mock_httpx_response(200, api_payload),
        )
        self.assertEqual(len(search_res.results), 2)
        self.assertFalse(search_res.results[0].is_terminated_or_completed)
        self.assertIn("product_quantity", search_res.results[0].missing_fields)
        self.assertIn("downstream_store_inventory", search_res.results[0].missing_fields)
        # Guard against openFDA's status quirk when termination_date is populated
        self.assertTrue(search_res.results[1].is_terminated_or_completed)

        # Ingest selected record into RecallRelay
        ingest_res = ingest_openfda_recall(
            self._db_path,
            OpenFdaIngestRequest(recall_number="F-0412-2026", run_source_analyst=True),
            http_get=lambda *a, **kw: _mock_httpx_response(
                200,
                {"meta": {"last_updated": "2026-11-18"}, "results": [sample_record]},
            ),
        )
        canonical = json.dumps(sample_record, sort_keys=True, ensure_ascii=True)
        expected_sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        self.assertEqual(ingest_res.payload_sha256, expected_sha)
        self.assertEqual(ingest_res.evidence.sha256_hash, expected_sha)
        self.assertEqual(ingest_res.evidence.evidence_stage.value, "SOURCE_MATERIAL")
        self.assertEqual(ingest_res.evidence.verification_status.value, "AWAITING_EVIDENCE")
        self.assertEqual(ingest_res.recall.severity.value, "CLASS_I")
        self.assertEqual(len(ingest_res.batches_created), 1)
        # Never invent an integer quantity when product_quantity is missing
        self.assertIsNone(ingest_res.batches_created[0].reported_quantity)
        self.assertIsNotNone(ingest_res.analyst_execution)

    # -------------------------------------------------------------------------
    # 5. Login Rate Limiting (No X-Forwarded-For Bypass) & Session Cleanup
    # -------------------------------------------------------------------------
    def test_login_rate_limiting_blocks_brute_force_and_ignores_spoofed_forwarded_ip(self) -> None:
        # 5 failed attempts from same peer IP (even with rotating X-Forwarded-For headers)
        for idx in range(5):
            resp = self.client.post(
                "/api/v1/session/login/admin",
                headers={
                    "Origin": "http://localhost:8000",
                    "X-Forwarded-For": f"203.0.113.{idx + 1}",
                },
                json={"admin_key": "wrong-key"},
            )
            self.assertEqual(resp.status_code, 401)

        # 6th attempt must be throttled with 429 and Retry-After even with another spoofed header
        blocked = self.client.post(
            "/api/v1/session/login/admin",
            headers={
                "Origin": "http://localhost:8000",
                "X-Forwarded-For": "198.51.100.99",
            },
            json={"admin_key": self.ADMIN_KEY},
        )
        self.assertEqual(blocked.status_code, 429)
        self.assertIn("Retry-After", blocked.headers)

        # Simulate time-window expiration and confirm legitimate login recovers
        with get_db_connection(self._db_path) as conn:
            conn.execute("DELETE FROM login_attempts")
        recovered = self.client.post(
            "/api/v1/session/login/admin",
            headers={"Origin": "http://localhost:8000"},
            json={"admin_key": self.ADMIN_KEY},
        )
        self.assertEqual(recovered.status_code, 200)

    def test_expired_and_old_revoked_session_cleanup_preserves_active_sessions(self) -> None:
        active_sess, active_tok = create_bff_session(
            self._db_path, principal_type="ADMIN", ttl_minutes=60
        )
        expired_sess, _ = create_bff_session(
            self._db_path, principal_type="ADMIN", ttl_minutes=60
        )
        revoked_sess, revoked_tok = create_bff_session(
            self._db_path, principal_type="ADMIN", ttl_minutes=60
        )
        revoke_bff_session_by_token(self._db_path, revoked_tok)

        old_time = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        with get_db_connection(self._db_path) as conn:
            conn.execute(
                "UPDATE bff_sessions SET expires_at = ? WHERE id = ?",
                (old_time, expired_sess.id),
            )
            conn.execute(
                "UPDATE bff_sessions SET revoked_at = ? WHERE id = ?",
                (old_time, revoked_sess.id),
            )

        deleted_count = purge_stale_bff_sessions(
            self._db_path, max_delete=50, revoked_retention_hours=24
        )
        self.assertEqual(deleted_count, 2)

        # Active session must remain completely intact and valid
        resolved = resolve_bff_session(self._db_path, active_tok)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.id, active_sess.id)


if __name__ == "__main__":
    unittest.main()
