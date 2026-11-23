"""Phase 9 Third Independent Audit regression tests (offline / stubbed unit tests).

NOTE ON HONESTY:
All external network calls (BAND Agent REST API, Lyzr v3 API, FDA HTML fetch) in this file
are deterministically stubbed for offline CI/unit verification. Live external verification
must be run locally via `scripts/band_smoke.py` and `scripts/lyzr_smoke.py` with real `.env` keys.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from app.agents.base import AgentAuthContext, AgentRole
from app.agents.specialists import build_agent
from app.agents.tools import ToolRegistry
from app.core.config import Settings
from app.db.database import (
    SCHEMA_VERSION_PHASE9_AUDIT3,
    get_db_connection,
    init_db,
    mark_stale_running_orchestrations_interrupted,
)
from app.integrations.band_adapter import BandMessageTransport
from app.integrations.lyzr_adapter import LyzrReasoningAdapter, verify_lyzr_grounding
from app.integrations.registry import IntegrationRegistry
from app.main import create_app
from app.schemas.api_v1 import EvidenceRegisterRequest, EvidenceStage, RecallCreateRequest
from app.schemas.domain import (
    EvidenceSourceType,
    FindingType,
    RecallSeverity,
    VerificationStatus,
    new_id,
    utc_now_iso,
)
from app.services import evidence_service, orchestration_service, recall_service
from app.services.fda_page_importer import (
    FdaPageImportRequest,
    detect_source_date_contradictions,
    import_fda_webpage,
)


class Phase9ThirdAuditTests(unittest.TestCase):
    """Offline unit tests covering all 14 third-audit fixes."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmpdir.name) / "third_audit.db")
        init_db(self.db_path)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _seed_naia_recall(self) -> tuple[str, str]:
        naia_text = (
            "Company Announcement Date: November 16, 2026\n"
            "FDA Publish Date: November 17, 2026\n"
            "Company Name: Naia, Inc.\n"
            "Product Description: Chocolate Banana Bar Sorbetto (3oz clear wrapper)\n"
            "Reason for Announcement: Undeclared Allergen - milk, eggs, tree nut (pistachio)\n"
            "Naia, Inc. is recalling Chocolate Banana Bar Sorbetto with code dates of "
            "10/5/2027, 10/6/2027, 10/7/2027, and 10/8/2026.\n"
            "The recalled product was distributed between October 7, 2026 and November 13, 2026.\n"
            'The recalled product has the date codes "BB:Oct/05/2027", "BB:Oct/06/2027", '
            '"BB:Oct/07/2027", or "BB:Oct/08/2027".\n'
        )
        ev = evidence_service.register_evidence(
            self.db_path,
            EvidenceRegisterRequest(
                source_type=EvidenceSourceType.OFFICIAL_PUBLIC_NOTICE,
                evidence_stage=EvidenceStage.SOURCE_MATERIAL,
                source_url="https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts/naia-inc-recalls-chocolate-banana-bar-sorbetto-because-undeclared-milk-eggs-and-nuts",
                document_content=naia_text,
                source_organization="U.S. Food and Drug Administration",
                publication_date="November 17, 2026",
                record_reference="fda-url-slug:naia-test",
                notes=naia_text,
            ),
        )
        rec = recall_service.create_recall(
            self.db_path,
            RecallCreateRequest(
                external_recall_number="fda-url-slug:naia-test",
                title="Naia, Inc. Recalls Chocolate Banana Bar Sorbetto",
                issuing_authority="U.S. Food and Drug Administration",
                product_description="Chocolate Banana Bar Sorbetto",
                reason="Undeclared Allergen - milk, eggs, tree nut (pistachio)",
                severity=RecallSeverity.UNKNOWN,
                status=VerificationStatus.AWAITING_EVIDENCE,
                evidence_id=ev.id,
                published_at="November 17, 2026",
            ),
        )
        return rec.id, ev.id

    def test_migration_010_and_startup_interrupted_recovery(self) -> None:
        with get_db_connection(self.db_path) as conn:
            mig = conn.execute(
                "SELECT version FROM schema_migrations WHERE version = ?",
                (SCHEMA_VERSION_PHASE9_AUDIT3,),
            ).fetchone()
            self.assertIsNotNone(mig)

        recall_id, _ = self._seed_naia_recall()
        stale_id = new_id("orch")
        with get_db_connection(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO orchestration_runs (
                    id, recall_id, requester_scope, status, active_transport,
                    lyzr_used, band_used, summary_json, created_at, completed_at
                ) VALUES (?, ?, 'admin', 'RUNNING', 'internal_sqlite_transport', 0, 0, '{}', ?, NULL)
                """,
                (stale_id, recall_id, utc_now_iso()),
            )
        recovered = mark_stale_running_orchestrations_interrupted(self.db_path)
        self.assertEqual(recovered, 1)
        run = orchestration_service.get_orchestration(self.db_path, stale_id)
        self.assertEqual(run.status, "INTERRUPTED")
        self.assertIsNotNone(run.completed_at)

    def test_fda_importer_main_content_dual_hashes_and_no_4000_char_truncation(self) -> None:
        padding = " ".join(["Authorized recall details paragraph."] * 250)  # > 9,000 chars
        html = (
            "<html><head><title>Naia, Inc. Recalls Sorbetto</title></head><body>"
            "<nav>Navigation menu 10/8/2025 Chrome noise</nav>"
            "<main><article>"
            "<p>Company Announcement Date: November 16, 2026</p>"
            "<p>FDA Publish Date: November 17, 2026</p>"
            "<p>Company Name: Naia, Inc.</p>"
            "<p>Product Description: Chocolate Banana Bar Sorbetto</p>"
            "<p>Reason for Announcement: Undeclared milk, eggs, pistachio</p>"
            f"<p>{padding}</p>"
            "<p>Recalled product has code dates of 10/5/2027, 10/6/2027, 10/7/2027, and 10/8/2026.</p>"
            '<p>The recalled product has the date codes "BB:Oct/05/2027", "BB:Oct/06/2027", "BB:Oct/07/2027", or "BB:Oct/08/2027".</p>'
            "</article></main>"
            "<footer>Footer copyright 2026</footer></body></html>"
        )
        fake_resp = httpx.Response(200, content=html.encode("utf-8"))
        out = import_fda_webpage(
            self.db_path,
            FdaPageImportRequest(
                url="https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts/naia-test",
                create_recall=True,
                run_source_analyst=False,
            ),
            http_get=lambda *a, **kw: fake_resp,
        )
        self.assertTrue(out.main_content_extracted)
        self.assertNotEqual(out.raw_html_sha256, out.extracted_text_sha256)
        self.assertGreater(out.extracted_text_length, 5000)
        self.assertEqual(len(out.source_date_contradictions), 1)
        self.assertEqual(out.source_date_contradictions[0].month_day, "10/08")
        # Date codes must NOT be misclassified as batch/lot identifiers
        self.assertEqual(
            out.extracted_fields.get("batch_association_status"),
            "UNRESOLVED_NO_EXPLICIT_LOT_ID",
        )
        self.assertIn("affected_batch_lot_identifiers", out.missing_fields)
        self.assertIn("BB:Oct/08/2027", out.extracted_fields.get("product_code_dates", []))

    def test_date_contradiction_ignores_announcement_and_distribution_dates(self) -> None:
        benign_text = (
            "Company Announcement Date: 10/8/2025.\n"
            "The product was distributed between October 8, 2026 and November 13, 2026.\n"
            "Consumers who purchased the item on 10/08/2024 should discard it.\n"
        )
        self.assertEqual(detect_source_date_contradictions(benign_text), [])

    def test_lyzr_grounding_token_boundaries_min_lengths_and_claim_merging(self) -> None:
        source = (
            "Product: Buttermilk Chocolate Banana Bar Sorbetto. "
            "Reason: Undeclared pistachio allergen. Lot #0160933."
        )
        lyzr_json = json.dumps(
            {
                "product_name": "Chocolate Banana Bar Sorbetto",
                "hazard": "milk",  # Mid-word inside 'Buttermilk' -> MUST be UNGROUNDED
                "lot_number": "01",  # Too short (< 3 chars) -> MUST be UNGROUNDED
                "distribution_locations": "Mars Colony",  # Hallucinated -> UNGROUNDED
            }
        )
        report = verify_lyzr_grounding(raw_lyzr_text=lyzr_json, source_text=source)
        self.assertIn("product_name", report["grounded_fields"])
        self.assertIn("hazard", report["ungrounded_fields"])
        self.assertIn("lot_number", report["ungrounded_fields"])
        self.assertIn("distribution_locations", report["ungrounded_fields"])

        # Test claim merging into RecallSourceAnalystAgent output
        ev = evidence_service.register_evidence(
            self.db_path,
            EvidenceRegisterRequest(
                source_type=EvidenceSourceType.OFFICIAL_PUBLIC_NOTICE,
                evidence_stage=EvidenceStage.SOURCE_MATERIAL,
                source_url="https://www.fda.gov/safety/recalls/test-lyzr",
                document_content=source,
                source_organization="FDA",
                record_reference="fda-lyzr-test",
                notes=source,
            ),
        )
        lyzr = LyzrReasoningAdapter(
            db_path=self.db_path,
            api_key="test-lyzr-secret-key",
            agent_id="lyzr-agent-1",
        )
        fake_http = httpx.Response(
            200,
            json={"response": lyzr_json, "session_id": "sess_123"},
            request=httpx.Request("POST", "https://agent-prod.studio.lyzr.ai/v3/inference/chat/"),
        )
        with patch("app.integrations.lyzr_adapter.httpx.post", return_value=fake_http):
            agent = build_agent(
                AgentRole.RECALL_SOURCE_ANALYST,
                db_path=self.db_path,
                tool_registry=ToolRegistry(),
                lyzr_adapter=lyzr,
            )
            env = agent.run(
                {"evidence_id": ev.id, "source_text": source},
                AgentAuthContext(is_admin=True, boundary=None),
            )
        claims = env.output["extracted_claims"]
        ai_claims = [c for c in claims if c["evidence_stage"] == "AI_EXTRACTED"]
        self.assertEqual(len(ai_claims), 1)
        self.assertEqual(ai_claims[0]["field_name"], "lyzr_product_name")
        self.assertEqual(ai_claims[0]["verification_status"], "AWAITING_EVIDENCE")

        # Verify persistent sponsor verification evidence survives adapter restart
        fresh_lyzr = LyzrReasoningAdapter(db_path=self.db_path)
        fresh_report = fresh_lyzr.status_report()
        self.assertIsNotNone(fresh_report.last_successful_live_operation)
        self.assertEqual(fresh_report.connection_state.value, "CONFIGURATION_MISSING")

    def test_band_receiver_executes_real_verification_agent_audited_and_deduplicated(self) -> None:
        recall_id, _ = self._seed_naia_recall()
        room_store: list[dict] = []

        def fake_send(**kwargs):
            msg_id = f"band_ext_{len(room_store) + 1}"
            room_store.append({"id": msg_id, "content": kwargs["content"]})
            return {"id": msg_id}

        def fake_list(**kwargs):
            return list(room_store)

        settings = Settings(
            database_path=self.db_path,
            a2a_transport="band",
            band_sender_agent_id="sender-uuid-1",
            band_sender_api_key="sender-secret-1",
            band_receiver_agent_id="receiver-uuid-2",
            band_receiver_api_key="receiver-secret-2",
            band_room_id="room-uuid-99",
            band_poll_attempts=3,
            band_poll_interval_seconds=0.0,
        )
        registry = IntegrationRegistry(db_path=self.db_path, settings=settings, a2a_transport_override="band")
        band = registry.get_adapter("band")
        assert isinstance(band, BandMessageTransport)
        band._sdk_send_fn = fake_send
        band._sdk_list_fn = fake_list

        run1 = orchestration_service.run_coordinated_response(
            self.db_path,
            orchestration_service.OrchestrationRunRequest(recall_id=recall_id),
            registry=registry,
        )
        self.assertTrue(run1.band_used)
        step7 = next(s for s in run1.steps if s.step_key == "a2a_band_communication")
        step8 = next(s for s in run1.steps if s.step_key == "independent_verification")
        self.assertTrue(step7.details.get("receiver_executed_real_agent"))
        self.assertEqual(step8.details.get("execution_source"), "band_two_way_receiver_execution")
        self.assertEqual(step7.details.get("receiver_execution_id"), step8.details.get("execution_id"))

        # Run a second orchestration on the same recall and confirm verification findings are deduplicated & audited
        run2 = orchestration_service.run_coordinated_response(
            self.db_path,
            orchestration_service.OrchestrationRunRequest(recall_id=recall_id),
            registry=registry,
        )
        self.assertNotEqual(run1.id, run2.id)
        with get_db_connection(self.db_path) as conn:
            findings = conn.execute(
                "SELECT * FROM verification_findings WHERE recall_id = ?", (recall_id,)
            ).fetchall()
            audits = conn.execute(
                "SELECT * FROM audit_events WHERE event_type = 'VERIFICATION_FINDING_CREATED'"
            ).fetchall()
        self.assertEqual(len(findings), 1)
        self.assertEqual(len(audits), 1)

        # Verify persistent BAND verification evidence survives restart
        fresh_band = BandMessageTransport(db_path=self.db_path)
        self.assertIsNotNone(fresh_band.status_report().last_successful_live_operation)

    def test_async_http_202_and_scoped_idempotency_and_concurrent_lock(self) -> None:
        recall_id, _ = self._seed_naia_recall()
        app = create_app(database_path=self.db_path, admin_api_key="test-admin-key")
        client = TestClient(app)

        # POST /api/v1/orchestrations/run returns HTTP 202 immediately
        resp = client.post(
            "/api/v1/orchestrations/run",
            headers={"X-Admin-Key": "test-admin-key"},
            json={"recall_id": recall_id, "idempotency_key": "idem-key-1"},
        )
        self.assertEqual(resp.status_code, 202)
        orch_id = resp.json()["id"]

        # Polling GET /api/v1/orchestrations/{id} returns completed state after BackgroundTasks finish
        poll = client.get(
            f"/api/v1/orchestrations/{orch_id}",
            headers={"X-Admin-Key": "test-admin-key"},
        )
        self.assertEqual(poll.status_code, 200)
        self.assertIn(poll.json()["status"], {"COMPLETED", "COMPLETED_WITH_GAPS"})

        # Scoped idempotency: same key + same recall + same requester reuses run
        reuse, should_exec = orchestration_service.start_coordinated_response(
            self.db_path,
            orchestration_service.OrchestrationRunRequest(
                recall_id=recall_id, idempotency_key="idem-key-1"
            ),
            requester_scope="admin",
        )
        self.assertFalse(should_exec)
        self.assertTrue(reuse.reused_idempotent_run)
        self.assertEqual(reuse.id, orch_id)

        # Concurrent lock: an active PENDING run blocks a second simultaneous run on the same recall
        active_run, should_exec2 = orchestration_service.start_coordinated_response(
            self.db_path,
            orchestration_service.OrchestrationRunRequest(recall_id=recall_id),
            requester_scope="admin",
        )
        self.assertTrue(should_exec2)
        self.assertEqual(active_run.status, "PENDING")
        with self.assertRaises(Exception) as ctx:
            orchestration_service.start_coordinated_response(
                self.db_path,
                orchestration_service.OrchestrationRunRequest(recall_id=recall_id),
                requester_scope="admin",
            )
        self.assertEqual(getattr(ctx.exception, "status_code", None), 409)

        # Exception safety: unexpected failure transitions run to FAILED (never stuck RUNNING)
        with patch(
            "app.services.orchestration_service.evidence_service.get_evidence_by_id",
            side_effect=RuntimeError("simulated boom"),
        ):
            failed_run = orchestration_service.execute_orchestration_in_background(
                self.db_path, active_run.id
            )
        self.assertEqual(failed_run.status, "FAILED")
        self.assertIsNotNone(failed_run.completed_at)


if __name__ == "__main__":
    unittest.main()
