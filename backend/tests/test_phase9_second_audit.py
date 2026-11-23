"""Phase 9 Continuation — Second Independent Audit Regression Suite.

NOTE: These are unit and in-process integration tests using local HTTP/SDK stubs.
They verify application logic, security controls, provenance, grounding, and persistence.
They do NOT constitute live external BAND, Lyzr, or fda.gov network verification.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from app.agents.base import AgentRole
from app.communication.schemas import A2AMessageCreate, A2AMessageType
from app.integrations.band_adapter import BandMessageTransport
from app.integrations.lyzr_adapter import LyzrReasoningAdapter, verify_lyzr_grounding
from app.main import create_app
from app.services.fda_page_importer import (
    FdaPageImportRequest,
    detect_source_date_contradictions,
    import_fda_webpage,
)

NAIA_FDA_URL = (
    "https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts/"
    "naia-inc-recalls-chocolate-banana-bar-sorbetto-because-undeclared-milk-eggs-and-nuts"
)

# Verbatim excerpts verified from the live Nov 17, 2026 FDA Naia, Inc. recall page
NAIA_VERBATIM_HTML = """<!doctype html>
<html>
<head><title>Naia, Inc. Recalls Chocolate Banana Bar Sorbetto Because of Undeclared Milk, Eggs, and Nuts | FDA</title></head>
<body>
<script>window.evil = "must be stripped";</script>
<h1>Naia, Inc. Recalls Chocolate Banana Bar Sorbetto Because of Undeclared Milk, Eggs, and Nuts</h1>
<p>Company Announcement Date: November 16, 2026</p>
<p>FDA Publish Date: November 17, 2026</p>
<p>Company Name: Naia, Inc.</p>
<p>Product Description: Chocolate Banana Bar Sorbetto</p>
<p>Reason for Announcement: Undeclared Allergen - milk, eggs, tree nut (pistachio)</p>
<p>Naia, Inc. of Hercules, CA is recalling Chocolate Banana Bar Sorbetto with code dates of 10/5/2027, 10/6/2027, 10/7/2027, and 10/8/2026, because it may contain undeclared tree nuts (pistachio), milk, and egg allergens.</p>
<p>The recalled products were available for consumers purchased from October 7, 2026 to November 13, 2026 in retail grocery stores, supermarkets, and other retailers located in Northern California and Reno, NV.</p>
<p>The recalled product is packaged in a 3oz clear wrapper. The recalled product has the date codes "BB:Oct/05/2027", "BB:Oct/06/2027", "BB:Oct/07/2027", or "BB:Oct/08/2027" printed in black directly on the clear wrapper.</p>
</body>
</html>
"""


def _mock_resp(status_code: int, content: bytes | str, url: str = NAIA_FDA_URL) -> httpx.Response:
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return httpx.Response(
        status_code=status_code,
        content=raw,
        request=httpx.Request("GET", url),
    )


class TestPhase9SecondAudit(unittest.TestCase):
    ADMIN_KEY = "second-audit-admin-key-2026"

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = str(Path(self._tmpdir.name) / "second_audit.db")
        self.app = create_app(
            database_path=self._db_path,
            admin_api_key=self.ADMIN_KEY,
        )
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _login_admin(self, client: TestClient | None = None) -> dict[str, str]:
        c = client or self.client
        resp = c.post(
            "/api/v1/session/login/admin",
            headers={"Origin": "http://localhost:8000"},
            json={"admin_key": self.ADMIN_KEY},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return {"X-CSRF-Token": resp.json()["csrf_token"], "Origin": "http://localhost:8000"}

    # -------------------------------------------------------------------------
    # Priority 1: Band Agent REST API & clean boot without band-sdk
    # -------------------------------------------------------------------------
    def test_band_agent_rest_api_boots_cleanly_without_band_sdk_in_requirements(self) -> None:
        req_text = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text(encoding="utf-8")
        self.assertNotIn("band-sdk", req_text)
        # App boots cleanly with internal_sqlite using Band Agent REST API without band-sdk
        health = self.client.get("/api/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["status"], "ok")

    # -------------------------------------------------------------------------
    # Priority 2: Real two-way BAND sender/receiver room communication
    # -------------------------------------------------------------------------
    def test_band_two_way_sender_receiver_reply_and_deduplication(self) -> None:
        headers = self._login_admin()
        ev = self.client.post(
            "/api/v1/evidence",
            headers=headers,
            json={
                "source_type": "OFFICIAL_PUBLIC_NOTICE",
                "evidence_stage": "SOURCE_MATERIAL",
                "source_url": NAIA_FDA_URL,
                "document_content": "Naia test",
                "source_organization": "FDA",
                "record_reference": "naia-band-two-way",
            },
        ).json()
        recall = self.client.post(
            "/api/v1/recalls",
            headers=headers,
            json={
                "external_recall_number": "fda-url-slug:naia-two-way",
                "title": "Naia Two-Way BAND Test",
                "issuing_authority": "FDA",
                "product_description": "Chocolate Banana Bar Sorbetto",
                "reason": "Undeclared milk, eggs, pistachio",
                "severity": "UNKNOWN",
                "evidence_id": ev["id"],
            },
        ).json()

        # Simulated shared BAND room backed by an in-memory list for unit testing
        shared_room_messages: list[dict] = []
        send_counter = {"n": 0}

        def stub_send(**kwargs):
            send_counter["n"] += 1
            msg_id = f"band_ext_{send_counter['n']}"
            rec = {"id": msg_id, "agent_id": kwargs["agent_id"], "room_id": kwargs["room_id"], "content": kwargs["content"]}
            shared_room_messages.append(rec)
            return rec

        def stub_list(**kwargs):
            return list(shared_room_messages)

        band = BandMessageTransport(
            db_path=self._db_path,
            api_key="sender-key-secret",
            agent_id="sender-uuid-001",
            receiver_api_key="receiver-key-secret",
            receiver_agent_id="receiver-uuid-002",
            room_id="room_naia_shared",
            sdk_send_fn=stub_send,
            sdk_list_fn=stub_list,
        )
        self.assertTrue(band.is_two_way_configured())

        msg_payload = A2AMessageCreate(
            conversation_id="conv_naia_2way",
            recall_id=recall["id"],
            sender_agent=AgentRole.RECALL_SCOPE_AGENT,
            recipient_agent=AgentRole.INDEPENDENT_VERIFICATION_AGENT,
            message_type=A2AMessageType.TASK_HANDOFF,
            payload={"recall_id": recall["id"]},
            evidence_references=[ev["id"]],
            idempotency_key="band-2way-key-1",
        )
        res = band.execute_two_way_handoff(
            msg_payload,
            sender_boundary_id=None,
            actor_id="admin",
            receiver_handler=lambda inbound: {"status": "AWAITING_EVIDENCE", "received_id": inbound["id"]},
        )
        self.assertEqual(res["external_message_id"], "band_ext_1")
        self.assertEqual(res["external_reply_message_id"], "band_ext_2")
        self.assertEqual(res["message"].external_message_id, "band_ext_1")
        self.assertEqual(res["message"].external_reply_message_id, "band_ext_2")
        self.assertEqual(res["message"].external_receiver_agent_id, "receiver-uuid-002")
        self.assertTrue(band.two_way_verified)

        # Replay with same idempotency key must NOT send duplicate messages
        replay = band.execute_two_way_handoff(
            msg_payload,
            sender_boundary_id=None,
            actor_id="admin",
            receiver_handler=lambda inbound: {"should_not_run": True},
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(send_counter["n"], 2)

    # -------------------------------------------------------------------------
    # Priority 3 & 7: Official FDA Webpage Importer + Naia Source Date Conflict
    # -------------------------------------------------------------------------
    def test_fda_page_importer_security_and_naia_date_contradiction(self) -> None:
        # Reject invalid URLs, HTTP, userinfo, non-443 ports, redirects, and >2 MiB bodies
        for bad_url in [
            "http://www.fda.gov/safety/recalls/test",
            "https://evil.example.com/safety/recalls/test",
            "https://user:pass@www.fda.gov/safety/recalls/test",
            "https://www.fda.gov:8443/safety/recalls/test",
            "https://www.fda.gov/",
        ]:
            with self.assertRaises(Exception):
                import_fda_webpage(
                    self._db_path,
                    FdaPageImportRequest(url=bad_url),
                    http_get=lambda *a, **kw: _mock_resp(200, NAIA_VERBATIM_HTML),
                )

        # Reject redirect (302)
        with self.assertRaises(Exception) as redir_ctx:
            import_fda_webpage(
                self._db_path,
                FdaPageImportRequest(url=NAIA_FDA_URL),
                http_get=lambda *a, **kw: _mock_resp(302, b""),
            )
        self.assertEqual(redir_ctx.exception.status_code, 502)

        # Reject oversized payload (>2 MiB)
        with self.assertRaises(Exception) as size_ctx:
            import_fda_webpage(
                self._db_path,
                FdaPageImportRequest(url=NAIA_FDA_URL),
                http_get=lambda *a, **kw: _mock_resp(200, b"x" * (2 * 1024 * 1024 + 10)),
            )
        self.assertEqual(size_ctx.exception.status_code, 502)

        # Valid official FDA Naia webpage import (preview mode first: create_recall=False)
        res = import_fda_webpage(
            self._db_path,
            FdaPageImportRequest(url=NAIA_FDA_URL, create_recall=False, run_source_analyst=True),
            http_get=lambda *a, **kw: _mock_resp(200, NAIA_VERBATIM_HTML),
        )
        self.assertIsNone(res.recall)
        self.assertEqual(res.evidence.evidence_stage.value, "SOURCE_MATERIAL")
        self.assertEqual(res.evidence.verification_status.value, "AWAITING_EVIDENCE")
        self.assertFalse(res.termination_notice_detected)
        self.assertNotIn("window.evil", res.evidence.notes or "")
        # Verifies genuine Naia date contradiction (10/8/2026 vs BB:Oct/08/2027) is preserved!
        self.assertEqual(len(res.source_date_contradictions), 1)
        c = res.source_date_contradictions[0]
        self.assertEqual(c.month_day, "10/08")
        self.assertIn("10/8/2026", c.conflicting_values)
        self.assertIn("BB:Oct/08/2027", c.conflicting_values)

    def test_fda_page_importer_real_echo_layout_and_naia_date_contradiction(self) -> None:
        # Real official FDA announcement page layout echoes labels:
        # "Product Description:" then "Product Description" then value
        # "Recall Reason Description:" then "Recall Reason Description" then value
        # "Brand Name(s):" then "Brand Name(s)" then value
        real_layout_html = """<!doctype html>
        <html>
        <head><title>Naia, Inc. Recalls Chocolate Banana Bar Sorbetto | FDA</title></head>
        <body>
        <main>
          <h1>Naia, Inc. Recalls Chocolate Banana Bar Sorbetto Because of Undeclared Milk, Eggs, and Nuts</h1>
          <p>Company Announcement Date:</p>
          <p>November 16, 2026</p>
          <p>FDA Publish Date:</p>
          <p>November 17, 2026</p>
          <p>Company Name:</p>
          <p>Company Name</p>
          <p>Naia, Inc.</p>
          <p>Brand Name(s):</p>
          <p>Brand Name(s)</p>
          <p>Naia</p>
          <p>Product Description:</p>
          <p>Product Description</p>
          <p>Chocolate Banana Bar Sorbetto</p>
          <p>Recall Reason Description:</p>
          <p>Recall Reason Description</p>
          <p>Undeclared tree nuts (pistachio), milk, and egg allergens</p>
          <p>Naia, Inc. of Hercules, CA is recalling Chocolate Banana Bar Sorbetto with code dates of 10/5/2027, 10/6/2027, 10/7/2027, and 10/8/2026, because it may contain undeclared tree nuts (pistachio), milk, and egg allergens.</p>
          <p>The recalled products were available for consumers purchased from October 7, 2026 to November 13, 2026 in retail grocery stores.</p>
          <p>The recalled product has the date codes "BB:Oct/05/2027", "BB:Oct/06/2027", "BB:Oct/07/2027", or "BB:Oct/08/2027" printed in black directly on the clear wrapper.</p>
        </main>
        </body>
        </html>
        """
        res = import_fda_webpage(
            self._db_path,
            FdaPageImportRequest(url=NAIA_FDA_URL, create_recall=True, run_source_analyst=False),
            http_get=lambda *a, **kw: _mock_resp(200, real_layout_html),
        )
        self.assertIsNotNone(res.recall)
        # Verify extraction skipped label-echo lines and took the next non-label line
        self.assertEqual(res.extracted_fields["product_description"], "Chocolate Banana Bar Sorbetto")
        self.assertNotEqual(res.extracted_fields["product_description"], "Product Description")
        self.assertEqual(res.extracted_fields["hazard_or_reason"], "Undeclared tree nuts (pistachio), milk, and egg allergens")
        self.assertNotEqual(res.extracted_fields["hazard_or_reason"], "Recall Reason Description")
        self.assertEqual(res.extracted_fields["brand_names"], "Naia")
        self.assertEqual(res.extracted_fields["company_name"], "Naia, Inc.")
        # Verify recall notice uses the extracted product description and reason
        self.assertEqual(res.recall.product_description, "Chocolate Banana Bar Sorbetto")
        self.assertIn("Undeclared tree nuts (pistachio), milk, and egg allergens", res.recall.reason)
        # Verify Naia 10/8/2026 vs BB:Oct/08/2027 contradiction is detected
        self.assertEqual(len(res.source_date_contradictions), 1)
        c = res.source_date_contradictions[0]
        self.assertEqual(c.month_day, "10/08")
        self.assertIn("10/8/2026", c.conflicting_values)
        self.assertIn("BB:Oct/08/2027", c.conflicting_values)

    def test_band_mention_target_and_compact_content_and_handle_fallback(self) -> None:
        from app.integrations.band_adapter import _default_band_rest_send
        from fastapi import HTTPException
        # Clear error when no mention target is configured
        with self.assertRaises(ValueError):
            _default_band_rest_send(
                api_key="key",
                agent_id="sender",
                room_id="room",
                content="test",
                mention_agent_id=None,
            )

        # In transport send_message without receiver agent id returns clear 400 error
        unconfigured_band = BandMessageTransport(
            db_path=self._db_path,
            api_key="key",
            agent_id="sender",
            receiver_agent_id=None,
        )
        msg_payload = A2AMessageCreate(
            conversation_id="conv_target_test",
            recall_id="rec_test",
            sender_agent=AgentRole.RECALL_SCOPE_AGENT,
            recipient_agent=AgentRole.INDEPENDENT_VERIFICATION_AGENT,
            message_type=A2AMessageType.TASK_HANDOFF,
            payload={"test": 123},
        )
        with self.assertRaises(HTTPException) as ctx:
            unconfigured_band.send_message(msg_payload, sender_boundary_id=None, actor_id="admin")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("mention target", str(ctx.exception.detail).lower())

        # Verify compact content formatting (no whitespace between separators)
        compact_content = unconfigured_band._format_room_content(msg_payload, "local_123")
        self.assertNotIn(" ", compact_content)
        parsed = json.loads(compact_content)
        self.assertEqual(parsed["local_message_id"], "local_123")
        self.assertEqual(parsed["payload"], {"test": 123})

    # -------------------------------------------------------------------------
    # Priority 4: Lyzr Verbatim-Span Grounding Verification
    # -------------------------------------------------------------------------
    def test_lyzr_grounding_excludes_ungrounded_and_preserves_disagreements(self) -> None:
        source_text = (
            "Naia, Inc. of Hercules, CA is recalling Chocolate Banana Bar Sorbetto "
            "because it may contain undeclared tree nuts (pistachio), milk, and egg allergens. "
            "Distributed in Northern California and Reno, NV."
        )
        # Lyzr returns 2 grounded fields + 1 hallucinated quantity ("50,000 cases")
        lyzr_json = json.dumps(
            {
                "product_name": "Chocolate Banana Bar Sorbetto",
                "distribution_locations": "Northern California and Reno, NV",
                "quantity": "50,000 cases",
            }
        )
        report = verify_lyzr_grounding(
            raw_lyzr_text=lyzr_json,
            source_text=source_text,
            deterministic_claims={"product_name": "Naia Chocolate Banana Bar"},
        )
        self.assertEqual(report["grounded_count"], 2)
        self.assertEqual(report["ungrounded_count"], 1)
        self.assertIn("product_name", report["grounded_fields"])
        self.assertIn("quantity", report["ungrounded_fields"])
        self.assertNotIn("quantity", report["grounded_fields"])
        self.assertTrue(report["ungrounded_fields"]["quantity"]["excluded_from_recall_record"])
        self.assertEqual(len(report["deterministic_disagreements"]), 1)
        self.assertFalse(report["can_self_verify"])

    # -------------------------------------------------------------------------
    # Priority 5: One-Click Coordinated Response Orchestration
    # -------------------------------------------------------------------------
    def test_one_click_orchestration_persists_timeline_idempotency_and_naia_contradiction(self) -> None:
        headers = self._login_admin()
        # Import genuine Naia HTML with create_recall=True
        imp = import_fda_webpage(
            self._db_path,
            FdaPageImportRequest(url=NAIA_FDA_URL, create_recall=True, run_source_analyst=False),
            http_get=lambda *a, **kw: _mock_resp(200, NAIA_VERBATIM_HTML),
        )
        assert imp.recall is not None

        run1 = self.client.post(
            "/api/v1/orchestrations/run",
            headers=headers,
            json={"recall_id": imp.recall.id, "idempotency_key": "orch-naia-001"},
        )
        self.assertEqual(run1.status_code, 202, run1.text)
        data = run1.json()
        self.assertFalse(data["reused_idempotent_run"])
        self.assertEqual(len(data["steps"]), 9)

        # Polling GET /api/v1/orchestrations/{id} returns completed persisted run after BackgroundTasks finish
        poll = self.client.get(f"/api/v1/orchestrations/{data['id']}", headers=headers)
        self.assertEqual(poll.status_code, 200)
        self.assertEqual(poll.json()["id"], data["id"])
        poll_data = poll.json()
        self.assertFalse(poll_data["summary"]["self_approved"])
        self.assertEqual(poll_data["summary"]["source_date_contradictions_found"], 1)
        self.assertIsNotNone(poll_data["summary"]["proposed_approval_id"])

        steps_by_key = {s["step_key"]: s for s in poll_data["steps"]}
        self.assertEqual(steps_by_key["evidence_retrieval"]["status"], "COMPLETED")
        self.assertEqual(steps_by_key["lyzr_evidence_analysis"]["status"], "SKIPPED")
        self.assertEqual(steps_by_key["organization_investigations"]["status"], "BLOCKED")
        self.assertEqual(steps_by_key["independent_verification"]["details"]["overall_status"], "DISCREPANCY")
        self.assertEqual(steps_by_key["response_coordinator_proposal"]["details"]["decision"], "PENDING")

        # Idempotent replay returns same orchestration ID without creating duplicate tasks/approvals
        run2 = self.client.post(
            "/api/v1/orchestrations/run",
            headers=headers,
            json={"recall_id": imp.recall.id, "idempotency_key": "orch-naia-001"},
        )
        self.assertEqual(run2.status_code, 202)
        self.assertTrue(run2.json()["reused_idempotent_run"])
        self.assertEqual(run2.json()["id"], data["id"])

    # -------------------------------------------------------------------------
    # Priority 8: Trusted Proxy CIDR/IP Handling & Spoofed X-Forwarded-For
    # -------------------------------------------------------------------------
    def test_trusted_proxy_headers_isolate_distinct_clients_and_block_spoofing(self) -> None:
        # Enable trusted proxy ONLY for TestClient's peer ("testclient")
        self.app.state.trust_proxy_headers = True
        self.app.state.trusted_proxy_ips = ["testclient"]

        # Exhaust 5 failures for Client A (203.0.113.10)
        for _ in range(5):
            r = self.client.post(
                "/api/v1/session/login/admin",
                headers={"Origin": "http://localhost:8000", "X-Forwarded-For": "203.0.113.10"},
                json={"admin_key": "wrong"},
            )
            self.assertEqual(r.status_code, 401)

        # Client A is locked out with 429 + Retry-After
        blocked_a = self.client.post(
            "/api/v1/session/login/admin",
            headers={"Origin": "http://localhost:8000", "X-Forwarded-For": "203.0.113.10"},
            json={"admin_key": self.ADMIN_KEY},
        )
        self.assertEqual(blocked_a.status_code, 429)
        self.assertIn("Retry-After", blocked_a.headers)

        # Client B (198.51.100.22) behind the same trusted Render proxy is NOT locked out!
        ok_b = self.client.post(
            "/api/v1/session/login/admin",
            headers={"Origin": "http://localhost:8000", "X-Forwarded-For": "198.51.100.22"},
            json={"admin_key": self.ADMIN_KEY},
        )
        self.assertEqual(ok_b.status_code, 200)

        # Client A cannot bypass lockout by prepending a spoofed IP on the left of X-Forwarded-For
        spoof_a = self.client.post(
            "/api/v1/session/login/admin",
            headers={"Origin": "http://localhost:8000", "X-Forwarded-For": "1.2.3.4, 203.0.113.10"},
            json={"admin_key": self.ADMIN_KEY},
        )
        self.assertEqual(spoof_a.status_code, 429)


if __name__ == "__main__":
    unittest.main()
