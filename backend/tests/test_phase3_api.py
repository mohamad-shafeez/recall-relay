"""Phase 3 automated tests updated for Phase 4 fail-closed administrative auth.

Uses abstract structural validation payloads only; no fabricated real-world business records.
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.db.database import connect, init_db
from app.main import create_app


class TestPhase3BackendAPI(unittest.TestCase):
    """End-to-end API, service, boundary-security, and DB integrity tests."""

    ADMIN_KEY = "test-admin-key-phase3-phase4"

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = str(Path(self._tmpdir.name) / "phase3_api_test.db")
        self.app = create_app(
            database_path=self._db_path, admin_api_key=self.ADMIN_KEY
        )
        self.client = TestClient(self.app)
        self.admin_headers = {"X-Admin-Key": self.ADMIN_KEY}

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _register_sample_evidence(
        self,
        *,
        record_reference: str = "REF-TEST-001",
        document_content: str | None = None,
    ) -> dict:
        body = {
            "source_type": "OFFICIAL_PUBLIC_NOTICE",
            "evidence_stage": "SOURCE_MATERIAL",
            "source_url": "https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts",
            "source_document": "official_notice_schema_test.txt"
            if document_content is not None
            else None,
            "document_content": document_content,
            "source_organization": "Official Public Authority (Test Ref)",
            "record_reference": record_reference,
            "verification_status": "AWAITING_EVIDENCE",
        }
        response = self.client.post(
            "/api/v1/evidence", json=body, headers=self.admin_headers
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def _register_org(self, name: str, role: str) -> tuple[dict, str]:
        response = self.client.post(
            "/api/v1/organizations",
            headers=self.admin_headers,
            json={
                "name": name,
                "role": role,
                "connection_status": "CONNECTED",
                "source_document": "org_registry_test_ref.json",
                "source_organization": "Authorization Registry (Test Ref)",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        data = response.json()
        return data["organization"], data["access_token"]

    def test_empty_state_lists_and_protected_reads(self) -> None:
        # Designated public read endpoint works without auth
        self.assertEqual(self.client.get("/api/v1/recalls").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/recalls").json(), [])

        # Protected internal directories fail closed without auth
        self.assertEqual(self.client.get("/api/v1/evidence").status_code, 401)
        self.assertEqual(self.client.get("/api/v1/organizations").status_code, 401)

        # Authorized empty reads return []
        self.assertEqual(
            self.client.get("/api/v1/evidence", headers=self.admin_headers).json(), []
        )
        self.assertEqual(
            self.client.get("/api/v1/organizations", headers=self.admin_headers).json(),
            [],
        )

    def test_sqlite_foreign_keys_enabled_on_every_connection_and_init_preserves_data(
        self,
    ) -> None:
        for _ in range(3):
            conn = connect(self._db_path)
            try:
                fk_val = conn.execute("PRAGMA foreign_keys").fetchone()[0]
                self.assertEqual(fk_val, 1)
            finally:
                conn.close()

        ev = self._register_sample_evidence(record_reference="REF-PERSIST-001")
        init_db(self._db_path)
        fetched = self.client.get(
            f"/api/v1/evidence/{ev['id']}", headers=self.admin_headers
        )
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json()["id"], ev["id"])

    def test_evidence_registration_hashing_and_stage_integrity(self) -> None:
        raw_content = "Official notice document bytes for hash verification"
        expected_hash = hashlib.sha256(raw_content.encode("utf-8")).hexdigest()

        created = self._register_sample_evidence(
            record_reference="REF-HASH-001", document_content=raw_content
        )
        self.assertEqual(created["sha256_hash"], expected_hash)
        self.assertEqual(created["evidence_stage"], "SOURCE_MATERIAL")
        self.assertEqual(created["verification_status"], "AWAITING_EVIDENCE")

        # Missing provenance -> 422
        missing_prov = self.client.post(
            "/api/v1/evidence",
            headers=self.admin_headers,
            json={
                "source_type": "OFFICIAL_PUBLIC_NOTICE",
                "source_organization": "Authority",
                "record_reference": "REF-BAD",
            },
        )
        self.assertEqual(missing_prov.status_code, 422)
        self.assertEqual(missing_prov.json()["error"]["code"], "validation_error")

        # AI_EXTRACTED cannot be marked VERIFIED -> 422
        ai_verified = self.client.post(
            "/api/v1/evidence",
            headers=self.admin_headers,
            json={
                "source_type": "OFFICIAL_PUBLIC_NOTICE",
                "evidence_stage": "AI_EXTRACTED",
                "source_document": "doc.txt",
                "source_organization": "Authority",
                "record_reference": "REF-AI-BAD",
                "verification_status": "VERIFIED",
            },
        )
        self.assertEqual(ai_verified.status_code, 422)

        # Clients cannot self-declare INDEPENDENTLY_VERIFIED -> 422
        self_verified = self.client.post(
            "/api/v1/evidence",
            headers=self.admin_headers,
            json={
                "source_type": "OFFICIAL_PUBLIC_NOTICE",
                "evidence_stage": "INDEPENDENTLY_VERIFIED",
                "source_document": "doc.txt",
                "source_organization": "Authority",
                "record_reference": "REF-SELF-VER-BAD",
                "verification_status": "AWAITING_EVIDENCE",
            },
        )
        self.assertEqual(self_verified.status_code, 422)

    def test_recall_and_batch_relationships_and_null_unknown_quantities(self) -> None:
        bad_recall = self.client.post(
            "/api/v1/recalls",
            headers=self.admin_headers,
            json={
                "external_recall_number": "EXT-MISSING-EV",
                "title": "Validation Recall Test",
                "issuing_authority": "Authority",
                "product_description": "Validation Product",
                "reason": "Validation Reason",
                "evidence_id": "ev_nonexistent",
            },
        )
        self.assertEqual(bad_recall.status_code, 404)
        self.assertEqual(bad_recall.json()["error"]["code"], "http_error")

        ev = self._register_sample_evidence(record_reference="REF-RECALL-001")
        recall_resp = self.client.post(
            "/api/v1/recalls",
            headers=self.admin_headers,
            json={
                "external_recall_number": "EXT-TEST-001",
                "title": "Schema Verification Notice",
                "issuing_authority": "Official Authority",
                "product_description": "Test Item Description",
                "reason": "Test Reason",
                "severity": "UNKNOWN",
                "status": "AWAITING_EVIDENCE",
                "evidence_id": ev["id"],
            },
        )
        self.assertEqual(recall_resp.status_code, 201)
        recall = recall_resp.json()

        dup_resp = self.client.post(
            "/api/v1/recalls",
            headers=self.admin_headers,
            json={
                "external_recall_number": "EXT-TEST-001",
                "title": "Duplicate Notice",
                "issuing_authority": "Official Authority",
                "product_description": "Test Item Description",
                "reason": "Test Reason",
                "evidence_id": ev["id"],
            },
        )
        self.assertEqual(dup_resp.status_code, 409)

        batch_resp = self.client.post(
            f"/api/v1/recalls/{recall['id']}/batches",
            headers=self.admin_headers,
            json={
                "batch_code": "LOT-SCHEMA-NULL",
                "product_name": "Test Item",
                "reported_quantity": None,
                "evidence_id": ev["id"],
            },
        )
        self.assertEqual(batch_resp.status_code, 201)
        batch = batch_resp.json()
        self.assertIsNone(batch["reported_quantity"])

        neg_batch = self.client.post(
            f"/api/v1/recalls/{recall['id']}/batches",
            headers=self.admin_headers,
            json={
                "batch_code": "LOT-SCHEMA-NEG",
                "product_name": "Test Item",
                "reported_quantity": -10,
                "evidence_id": ev["id"],
            },
        )
        self.assertEqual(neg_batch.status_code, 422)

    def test_fail_closed_organization_boundary_and_cross_org_isolation(self) -> None:
        self.assertEqual(self.client.get("/api/v1/shipments").status_code, 401)
        self.assertEqual(self.client.get("/api/v1/inventory").status_code, 401)

        bad_headers = {"Authorization": "Bearer rr_org_invalid_token"}
        self.assertEqual(
            self.client.get("/api/v1/shipments", headers=bad_headers).status_code, 401
        )

        ev = self._register_sample_evidence(record_reference="REF-BOUNDARY-001")
        org_a, token_a = self._register_org("Boundary Entity A", "MANUFACTURER")
        org_b, token_b = self._register_org("Boundary Entity B", "DISTRIBUTOR")
        self.assertEqual(org_a["identity_verification_status"], "UNVERIFIED")
        self.assertEqual(org_b["identity_verification_status"], "UNVERIFIED")
        headers_a = {"Authorization": f"Bearer {token_a}"}
        headers_b = {"Authorization": f"Bearer {token_b}"}

        recall = self.client.post(
            "/api/v1/recalls",
            headers=self.admin_headers,
            json={
                "external_recall_number": "EXT-BND-001",
                "title": "Boundary Isolation Verification",
                "issuing_authority": "Official Authority",
                "product_description": "Boundary Test Item",
                "reason": "Boundary Verification",
                "manufacturer_org_id": org_a["id"],
                "evidence_id": ev["id"],
            },
        ).json()

        batch = self.client.post(
            f"/api/v1/recalls/{recall['id']}/batches",
            headers=self.admin_headers,
            json={
                "batch_code": "LOT-BND-01",
                "product_name": "Boundary Test Item",
                "reported_quantity": None,
                "evidence_id": ev["id"],
            },
        ).json()

        self_ship = self.client.post(
            "/api/v1/shipments",
            headers=headers_a,
            json={
                "recall_id": recall["id"],
                "batch_id": batch["id"],
                "to_org_id": org_a["id"],
                "quantity_shipped": None,
                "evidence_id": ev["id"],
            },
        )
        self.assertEqual(self_ship.status_code, 400)

        ship_resp = self.client.post(
            "/api/v1/shipments",
            headers=headers_a,
            json={
                "recall_id": recall["id"],
                "batch_id": batch["id"],
                "to_org_id": org_b["id"],
                "from_org_id": org_b["id"],
                "owner_boundary_id": org_b["data_boundary_id"],
                "quantity_shipped": None,
                "evidence_id": ev["id"],
            },
        )
        self.assertEqual(ship_resp.status_code, 201)
        shipment = ship_resp.json()
        self.assertEqual(shipment["from_org_id"], org_a["id"])
        self.assertEqual(shipment["owner_boundary_id"], org_a["data_boundary_id"])
        self.assertIsNone(shipment["quantity_shipped"])

        inv_resp = self.client.post(
            "/api/v1/inventory",
            headers=headers_b,
            json={
                "recall_id": recall["id"],
                "batch_id": batch["id"],
                "quantity_on_hand": None,
                "quantity_quarantined": None,
                "inventory_status": "AWAITING_EVIDENCE",
                "evidence_id": ev["id"],
            },
        )
        self.assertEqual(inv_resp.status_code, 201)
        inventory = inv_resp.json()
        self.assertEqual(inventory["organization_id"], org_b["id"])
        self.assertEqual(inventory["owner_boundary_id"], org_b["data_boundary_id"])
        self.assertIsNone(inventory["quantity_on_hand"])

        self.assertEqual(
            self.client.get("/api/v1/shipments", headers=headers_b).json(), []
        )
        self.assertEqual(
            self.client.get(
                f"/api/v1/shipments/{shipment['id']}", headers=headers_b
            ).status_code,
            403,
        )

        self.assertEqual(
            self.client.get("/api/v1/inventory", headers=headers_a).json(), []
        )
        self.assertEqual(
            self.client.get(
                f"/api/v1/inventory/{inventory['id']}", headers=headers_a
            ).status_code,
            403,
        )

        self.client.post(f"/api/v1/recalls/{recall['id']}/public-disclosure?approved=true", headers=self.admin_headers)
        anon_trace = self.client.get(f"/api/v1/recalls/{recall['id']}/traceability")
        self.assertEqual(anon_trace.status_code, 200)
        t_anon = anon_trace.json()
        self.assertIsNone(t_anon["caller_boundary_id"])
        self.assertEqual(t_anon["visible_shipments"], [])
        self.assertEqual(t_anon["visible_inventory"], [])
        self.assertEqual(t_anon["traceability_status"], "NOT_CONNECTED")

        org_a_trace = self.client.get(
            f"/api/v1/recalls/{recall['id']}/traceability", headers=headers_a
        )
        self.assertEqual(org_a_trace.status_code, 200)
        t_a = org_a_trace.json()
        self.assertEqual(t_a["caller_boundary_id"], org_a["data_boundary_id"])
        self.assertEqual(len(t_a["visible_shipments"]), 1)
        self.assertEqual(t_a["visible_inventory"], [])


if __name__ == "__main__":
    unittest.main()
