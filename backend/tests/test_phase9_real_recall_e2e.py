"""Phase 9 end-to-end verification tests using a GENUINE official public FDA recall notice
and testing upgrade migration preservation from an existing Phase 2 database.

Genuine Official Public Source Used (Primary FDA Page Verified):
- URL: https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts/lehi-valley-trading-company-issues-allergy-alert-undeclared-peanuts-chocolate-covered-raisins
- Issuing Authority: U.S. Food and Drug Administration (Company Announcement — Lehi Valley Trading Company)
- Company Announcement / FDA Publish Date: 2026-06-25 (June 25, 2026)
- FDA Termination Notice on Page: "This recall has been completed and FDA has terminated this recall."
- Brand & Product: High Valley Orchard Chocolate Covered Raisins (15 ounce, clear plastic package)
- Batch / Lot Explicitly Stated: Lot # 0160933, Best By Jan 23, 2027, 624 units of 15 ounce packages
- Reason Explicitly Stated: May Contain Undeclared Peanuts ("because they contain undeclared peanuts")
- Distribution Statement: "distributed in retail stores through Albertson's distribution center between 5/18/2026 and 6/25/2026"
- Explicitly ABSENT from Official FDA Page:
  * No official FDA recall event number is published on the page (the verbatim FDA URL slug is used as a reference key).
  * No FDA Class I/II/III severity rating is published on the page (severity is therefore recorded as UNKNOWN).
  * No store-level shipment manifests, store inventory counts, or store acknowledgements are published.

Strictly creates ZERO fabricated manufacturer, distributor, or retailer shipment/inventory records
and verifies that RecallRelay explicitly surfaces those unresolved traceability gaps.
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.db.database import (
    SCHEMA_VERSION,
    SCHEMA_VERSION_PHASE2,
    get_db_connection,
    init_db,
)
from app.main import create_app

GENUINE_FDA_URL = (
    "https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts/"
    "lehi-valley-trading-company-issues-allergy-alert-undeclared-peanuts-chocolate-covered-raisins"
)
# The official FDA announcement page does not publish a numeric FDA recall event ID;
# use the verbatim FDA URL slug as an explicit non-fabricated reference identifier.
GENUINE_FDA_URL_SLUG_REF = (
    "fda-url-slug:lehi-valley-trading-company-issues-allergy-alert-undeclared-peanuts-chocolate-covered-raisins"
)
GENUINE_FDA_EXCERPT = (
    "Title: Lehi Valley Trading Company Issues Allergy Alert on Undeclared Peanuts in Chocolate Covered Raisins\n"
    "FDA Status Notice: This recall has been completed and FDA has terminated this recall.\n"
    "Product: High Valley Orchard Chocolate Covered Raisins (15 ounce, clear plastic package)\n"
    "Lot: 0160933\n"
    "Hazard: May Contain Undeclared Peanuts\n"
    "Date: 2026-06-25\n"
    "Quantity: 624 units of 15 ounce packages, Best By Jan 23, 2027.\n"
    "Distribution: distributed in retail stores through Albertson's distribution center between 5/18/2026 and 6/25/2026."
)


class TestPhase9RealRecallWorkflowAndMigrations(unittest.TestCase):
    """End-to-end verification on a genuine FDA recall record and Phase 2 -> Phase 9 migration."""

    ADMIN_KEY = "phase9-final-verification-key"

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = str(Path(self._tmpdir.name) / "phase9_real_recall.db")
        self.app = create_app(
            database_path=self._db_path, admin_api_key=self.ADMIN_KEY
        )
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_migration_from_existing_phase2_database_preserves_records(self) -> None:
        # Build a standalone Phase 2-only database containing a real FDA evidence row
        legacy_db = str(Path(self._tmpdir.name) / "legacy_phase2.db")
        schema_sql = (
            Path(__file__).resolve().parents[1] / "app" / "db" / "schema.sql"
        ).read_text(encoding="utf-8")
        with get_db_connection(legacy_db) as conn:
            conn.executescript(schema_sql)
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION_PHASE2, "2026-11-21T00:00:00+00:00"),
            )
            conn.execute(
                """
                INSERT INTO evidence_records (
                    id, source_type, source_url, source_document, source_organization,
                    publication_date, retrieved_at, record_reference, verification_status,
                    sha256_hash, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "ev_legacy_fda_001",
                    "OFFICIAL_PUBLIC_NOTICE",
                    GENUINE_FDA_URL,
                    None,
                    "U.S. Food and Drug Administration",
                    "2026-06-25",
                    "2026-11-21T00:00:00+00:00",
                    GENUINE_FDA_URL_SLUG_REF,
                    "AWAITING_EVIDENCE",
                    None,
                    "Preserved across Phase 2 -> Phase 9 migration. Official FDA page publishes no event ID.",
                ),
            )

        # Upgrade via init_db() through migrations 003 -> 007
        final_ver = init_db(legacy_db)
        self.assertEqual(final_ver, SCHEMA_VERSION)

        with get_db_connection(legacy_db) as conn:
            row = conn.execute(
                "SELECT id, evidence_stage FROM evidence_records WHERE id = ?",
                ("ev_legacy_fda_001",),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["evidence_stage"], "SOURCE_MATERIAL")

    def test_genuine_fda_recall_end_to_end_workflow_over_bff_session(self) -> None:
        # 1. Authenticate via BFF HttpOnly cookie session (no raw key stored in client after login)
        login = self.client.post(
            "/api/v1/session/login/admin",
            json={"admin_key": self.ADMIN_KEY},
        )
        self.assertEqual(login.status_code, 200, login.text)
        csrf = login.json()["csrf_token"]
        mut_headers = {
            "X-CSRF-Token": csrf,
            "Origin": "http://localhost:5173",
        }

        # 2. Register genuine FDA public recall evidence with SHA-256 hash
        ev_resp = self.client.post(
            "/api/v1/evidence",
            headers=mut_headers,
            json={
                "source_type": "OFFICIAL_PUBLIC_NOTICE",
                "evidence_stage": "SOURCE_MATERIAL",
                "source_url": GENUINE_FDA_URL,
                "document_content": GENUINE_FDA_EXCERPT,
                "source_organization": "U.S. Food and Drug Administration",
                "publication_date": "2026-06-25",
                "record_reference": GENUINE_FDA_URL_SLUG_REF,
                "notes": GENUINE_FDA_EXCERPT,
            },
        )
        self.assertEqual(ev_resp.status_code, 201, ev_resp.text)
        ev = ev_resp.json()
        self.assertEqual(
            ev["sha256_hash"],
            hashlib.sha256(GENUINE_FDA_EXCERPT.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(ev["verification_status"], "AWAITING_EVIDENCE")

        # 3. Run Recall Source Analyst (deterministic extraction, never self-verifies)
        analyst = self.client.post(
            "/api/v1/agents/RECALL_SOURCE_ANALYST/run",
            headers=mut_headers,
            json={"payload": {"evidence_id": ev["id"]}},
        )
        self.assertEqual(analyst.status_code, 200, analyst.text)
        analyst_out = analyst.json()["output"]
        extracted_fields = {c["field_name"]: c["value"] for c in analyst_out["extracted_claims"]}
        self.assertEqual(extracted_fields.get("batch_code"), "0160933")
        self.assertEqual(analyst_out["evidence_verification_status"], "AWAITING_EVIDENCE")

        # 4. Create official recall notice & link explicitly stated batch 0160933 (624 units of 15 oz packages)
        # Severity is UNKNOWN because the official FDA announcement page does not publish a Class I/II/III rating.
        recall_resp = self.client.post(
            "/api/v1/recalls",
            headers=mut_headers,
            json={
                "external_recall_number": GENUINE_FDA_URL_SLUG_REF,
                "title": "Lehi Valley Trading Company Issues Allergy Alert on Undeclared Peanuts in Chocolate Covered Raisins",
                "issuing_authority": "U.S. Food and Drug Administration",
                "product_description": "High Valley Orchard Chocolate Covered Raisins, 15 ounce clear plastic package",
                "reason": "May Contain Undeclared Peanuts (FDA page notes this recall has been completed and FDA has terminated this recall)",
                "severity": "UNKNOWN",
                "status": "AWAITING_EVIDENCE",
                "evidence_id": ev["id"],
                "published_at": "2026-06-25",
            },
        )
        self.assertEqual(recall_resp.status_code, 201, recall_resp.text)
        recall = recall_resp.json()
        self.assertEqual(recall["severity"], "UNKNOWN")

        batch_resp = self.client.post(
            f"/api/v1/recalls/{recall['id']}/batches",
            headers=mut_headers,
            json={
                "batch_code": "0160933",
                "product_name": "High Valley Orchard Chocolate Covered Raisins (15 ounce clear plastic package)",
                "expiration_or_best_by": "Jan 23, 2027",
                "reported_quantity": 624,
                "quantity_unit": "15 ounce packages",
                "evidence_id": ev["id"],
            },
        )
        self.assertEqual(batch_resp.status_code, 201, batch_resp.text)

        # Approve public disclosure for this genuine official FDA notice
        pub_resp = self.client.post(
            f"/api/v1/recalls/{recall['id']}/public-disclosure?approved=true",
            headers=mut_headers,
        )
        self.assertEqual(pub_resp.status_code, 200)

        # 5. Dispatch durable A2A handoff to Recall Scope Agent & process inbox
        handoff = self.client.post(
            "/api/v1/a2a/handoffs",
            headers=mut_headers,
            json={
                "recall_id": recall["id"],
                "conversation_id": "conv_fda_lehi_2026",
                "sender_agent": "RECALL_SOURCE_ANALYST",
                "recipient_agent": "RECALL_SCOPE_AGENT",
                "requested_action": "evaluate_fda_recall_scope",
                "required_output_schema": "RecallScopeOutput",
                "payload": {"recall_id": recall["id"]},
                "evidence_references": [ev["id"]],
            },
        )
        self.assertEqual(handoff.status_code, 201, handoff.text)

        proc = self.client.post(
            "/api/v1/a2a/inbox/RECALL_SCOPE_AGENT/process",
            headers=mut_headers,
        )
        self.assertEqual(proc.status_code, 200, proc.text)
        self.assertTrue(proc.json()["processed"])
        self.assertEqual(proc.json()["result_message"]["message_type"], "RESULT_REPORT")

        # 6. Verify Traceability explicitly reports unresolved downstream gaps (ZERO fabricated orgs/shipments)
        trace = self.client.get(f"/api/v1/recalls/{recall['id']}/traceability").json()
        self.assertEqual(len(trace["batches"]), 1)
        self.assertEqual(trace["visible_shipments"], [])
        self.assertEqual(trace["visible_inventory"], [])
        self.assertEqual(trace["total_recorded_shipments_in_system"], 0)
        self.assertEqual(trace["total_recorded_inventory_in_system"], 0)
        gap_types = {
            g["gap_type"]
            for g in (trace["missing_information"] + trace["unresolved_relationships"])
        }
        self.assertIn("NO_DOWNSTREAM_SHIPMENTS", gap_types)
        self.assertIn("NO_INVENTORY_REPORTS", gap_types)
        self.assertIn("UNLINKED_MANUFACTURER", gap_types)

        # 7. Independent Verification Agent honestly flags missing corroborating evidence
        ver = self.client.post(
            "/api/v1/agents/INDEPENDENT_VERIFICATION_AGENT/run",
            headers=mut_headers,
            json={
                "payload": {
                    "recall_id": recall["id"],
                    "primary_evidence_id": ev["id"],
                    "corroborating_evidence_id": None,
                    "expected_quantity": 624,
                    "reported_quantity": None,
                }
            },
        ).json()
        self.assertEqual(ver["output"]["overall_status"], "AWAITING_EVIDENCE")
        self.assertFalse(ver["output"]["policy_satisfied_for_verified"])

        # 8. Response Coordinator prepares report & proposes PENDING human approval
        coord = self.client.post(
            "/api/v1/agents/RECALL_RESPONSE_COORDINATOR/run",
            headers=mut_headers,
            json={"payload": {"recall_id": recall["id"], "propose_human_review_action": True}},
        ).json()
        apr_id = coord["output"]["proposed_approvals"][0]["id"]
        self.assertEqual(coord["output"]["proposed_approvals"][0]["decision"], "PENDING")

        # 9. Human admin records decision over BFF session & verifies intact SHA-256 audit chain
        dec = self.client.post(
            f"/api/v1/approvals/{apr_id}/decision",
            headers=mut_headers,
            json={
                "decision": "APPROVED",
                "reviewer_name": "Recall Operations Lead",
                "rationale": "Confirmed official FDA source URL, UNKNOWN severity on announcement page, and documented downstream data gaps.",
            },
        )
        self.assertEqual(dec.status_code, 200, dec.text)
        self.assertEqual(dec.json()["decision"], "APPROVED")

        chain = self.client.get("/api/v1/a2a/audit-events/verify").json()
        self.assertTrue(chain["valid"])
        self.assertGreaterEqual(chain["event_count"], 4)


if __name__ == "__main__":
    unittest.main()
