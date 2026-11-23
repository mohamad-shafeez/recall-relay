"""Phase 2 tests for domain schemas, provenance validation, and SQLite constraints.

Strictly tests pure validation rules, empty states, foreign keys, and CHECK
constraints without introducing fabricated business records.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from app.db.database import SCHEMA_VERSION, check_db_health, get_db_connection, init_db
from app.schemas.domain import (
    AcknowledgementCreate,
    AcknowledgementStatus,
    AffectedBatchCreate,
    EvidenceRecordCreate,
    EvidenceSourceType,
    InventoryRecordCreate,
    OrganizationCreate,
    OrganizationRole,
    ShipmentRecordCreate,
    VerificationFindingCreate,
    VerificationStatus,
    FindingType,
)


class TestDomainSchemaValidation(unittest.TestCase):
    """Verify strict real-data provenance and non-negative validation rules."""

    def test_evidence_requires_source_url_or_document(self) -> None:
        with self.assertRaises(ValidationError):
            EvidenceRecordCreate(
                source_type=EvidenceSourceType.OFFICIAL_PUBLIC_NOTICE,
                source_organization="U.S. Food and Drug Administration",
                record_reference="REF-VALIDATION-TEST",
            )

    def test_evidence_rejects_invalid_url_scheme(self) -> None:
        with self.assertRaises(ValidationError):
            EvidenceRecordCreate(
                source_type=EvidenceSourceType.OFFICIAL_PUBLIC_NOTICE,
                source_url="ftp://example.invalid/notice",
                source_organization="U.S. Food and Drug Administration",
                record_reference="REF-VALIDATION-TEST",
            )

    def test_evidence_accepts_https_url_and_defaults_to_awaiting_evidence(self) -> None:
        record = EvidenceRecordCreate(
            source_type=EvidenceSourceType.OFFICIAL_PUBLIC_NOTICE,
            source_url="https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts",
            source_organization="U.S. Food and Drug Administration",
            record_reference="FDA-PORTAL-REF",
        )
        self.assertEqual(
            record.verification_status, VerificationStatus.AWAITING_EVIDENCE
        )

    def test_organization_requires_provenance(self) -> None:
        with self.assertRaises(ValidationError):
            OrganizationCreate(
                name="Validation Boundary Check",
                role=OrganizationRole.REGULATOR,
                data_boundary_id="boundary_test_01",
                source_organization="Official Registry",
            )

    def test_batch_rejects_negative_quantity_and_allows_unknown_none(self) -> None:
        batch = AffectedBatchCreate(
            recall_id="rec_schema_check",
            batch_code="CODE-SCHEMA-CHECK",
            product_name="Schema Validation Item",
            reported_quantity=None,
            evidence_id="ev_schema_check",
        )
        self.assertIsNone(batch.reported_quantity)

        with self.assertRaises(ValidationError):
            AffectedBatchCreate(
                recall_id="rec_schema_check",
                batch_code="CODE-SCHEMA-CHECK",
                product_name="Schema Validation Item",
                reported_quantity=-1,
                evidence_id="ev_schema_check",
            )

    def test_shipment_rejects_self_transfer_and_negative_quantity(self) -> None:
        with self.assertRaises(ValidationError):
            ShipmentRecordCreate(
                recall_id="rec_schema_check",
                batch_id="bat_schema_check",
                from_org_id="org_same",
                to_org_id="org_same",
                owner_boundary_id="boundary_a",
                evidence_id="ev_schema_check",
            )

        with self.assertRaises(ValidationError):
            ShipmentRecordCreate(
                recall_id="rec_schema_check",
                batch_id="bat_schema_check",
                from_org_id="org_a",
                to_org_id="org_b",
                owner_boundary_id="boundary_a",
                quantity_shipped=-5,
                evidence_id="ev_schema_check",
            )

    def test_inventory_rejects_negative_quantities(self) -> None:
        with self.assertRaises(ValidationError):
            InventoryRecordCreate(
                recall_id="rec_schema_check",
                batch_id="bat_schema_check",
                organization_id="org_a",
                owner_boundary_id="boundary_a",
                quantity_on_hand=-1,
                evidence_id="ev_schema_check",
            )

    def test_acknowledgement_requires_evidence_when_acknowledged(self) -> None:
        # Pending / awaiting evidence without evidence_id is allowed
        pending = AcknowledgementCreate(
            recall_id="rec_schema_check",
            organization_id="org_a",
            status=AcknowledgementStatus.AWAITING_EVIDENCE,
        )
        self.assertIsNone(pending.evidence_id)

        # Claiming ACKNOWLEDGED without traceable evidence_id must fail
        with self.assertRaises(ValidationError):
            AcknowledgementCreate(
                recall_id="rec_schema_check",
                organization_id="org_a",
                status=AcknowledgementStatus.ACKNOWLEDGED,
                evidence_id=None,
            )

    def test_verification_finding_requires_non_empty_summary(self) -> None:
        with self.assertRaises(ValidationError):
            VerificationFindingCreate(
                recall_id="rec_schema_check",
                finding_type=FindingType.EVIDENCE_MISSING,
                summary="   ",
            )


class TestSQLiteSchemaAndConstraints(unittest.TestCase):
    """Verify idempotent SQLite initialization, empty states, and FK/CHECK constraints."""

    EXPECTED_TABLES = {
        "schema_migrations",
        "organizations",
        "evidence_records",
        "recall_notices",
        "affected_batches",
        "shipment_records",
        "inventory_records",
        "acknowledgements",
        "verification_findings",
        "agent_messages",
        "human_approvals",
    }

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = str(Path(self._tmpdir.name) / "phase2_test.db")
        init_db(self._db_path)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_schema_initialization_is_idempotent_and_healthy(self) -> None:
        version_second_run = init_db(self._db_path)
        self.assertEqual(version_second_run, SCHEMA_VERSION)
        self.assertTrue(check_db_health(self._db_path))

        with get_db_connection(self._db_path) as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            table_names = {row["name"] for row in rows}
            self.assertTrue(self.EXPECTED_TABLES.issubset(table_names))

    def test_fresh_database_has_clean_empty_business_tables(self) -> None:
        business_tables = self.EXPECTED_TABLES - {"schema_migrations"}
        with get_db_connection(self._db_path) as conn:
            for table in sorted(business_tables):
                count = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
                self.assertEqual(count, 0, f"Expected empty table {table} on fresh init")

    def test_foreign_key_constraint_blocks_recall_without_evidence(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            with get_db_connection(self._db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO recall_notices (
                        id, external_recall_number, title, issuing_authority,
                        product_description, reason, severity, status,
                        manufacturer_org_id, evidence_id, published_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "rec_fk_test",
                        "EXT-FK-TEST",
                        "FK Constraint Test",
                        "Official Authority",
                        "Product Description",
                        "Reason",
                        "UNKNOWN",
                        "AWAITING_EVIDENCE",
                        None,
                        "ev_missing_fk",
                        None,
                        "2026-11-21T00:00:00+00:00",
                        "2026-11-21T00:00:00+00:00",
                    ),
                )

    def test_check_constraint_blocks_evidence_without_url_or_document(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            with get_db_connection(self._db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO evidence_records (
                        id, source_type, source_url, source_document,
                        source_organization, publication_date, retrieved_at,
                        record_reference, verification_status, sha256_hash, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "ev_check_test",
                        "OFFICIAL_PUBLIC_NOTICE",
                        None,
                        None,
                        "Official Authority",
                        None,
                        "2026-11-21T00:00:00+00:00",
                        "REF-01",
                        "AWAITING_EVIDENCE",
                        None,
                        None,
                    ),
                )


if __name__ == "__main__":
    unittest.main()
