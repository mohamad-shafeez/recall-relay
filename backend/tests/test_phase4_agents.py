"""Phase 4 automated tests for security hardening, tool permissions, all 7 agents,
boundary enforcement, contradictory evidence, bounded timeouts, idempotency, and auditing.

Uses abstract structural validation payloads only; no fabricated business records.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from typing import List

from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.agents.audit import list_execution_audits
from app.agents.base import (
    AgentAuthContext,
    AgentRole,
    BaseAgent,
    ToolPermissionError,
)
from app.agents.tools import ToolRegistry
from app.main import create_app


class _SlowTestInput(BaseModel):
    ref_id: str


class _SlowTestOutput(BaseModel):
    ok: bool


class _SlowTimeoutAgent(BaseAgent[_SlowTestInput, _SlowTestOutput]):
    """Test-only agent subclass used strictly to verify bounded timeout enforcement."""

    agent_id = "agent_timeout_probe"
    role = AgentRole.RECALL_SCOPE_AGENT
    input_schema = _SlowTestInput
    output_schema = _SlowTestOutput

    def extract_input_references(self, validated_input: _SlowTestInput) -> List[str]:
        return [validated_input.ref_id]

    def extract_output_and_evidence_references(
        self, validated_output: _SlowTestOutput
    ) -> tuple[List[str], List[str]]:
        return [], []

    def execute(
        self, validated_input: _SlowTestInput, auth: AgentAuthContext
    ) -> _SlowTestOutput:
        time.sleep(0.3)
        return _SlowTestOutput(ok=True)


class TestPhase4AgentsAndSecurity(unittest.TestCase):
    """Verify Phase 4 security fixes, all 7 specialist agents, permissions, and auditing."""

    ADMIN_KEY = "test-admin-secret-phase4"

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = str(Path(self._tmpdir.name) / "phase4_test.db")
        self.app = create_app(
            database_path=self._db_path, admin_api_key=self.ADMIN_KEY
        )
        self.client = TestClient(self.app)
        self.admin_headers = {"X-Admin-Key": self.ADMIN_KEY}

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _create_evidence(self, ref: str, notes: str | None = None) -> dict:
        resp = self.client.post(
            "/api/v1/evidence",
            headers=self.admin_headers,
            json={
                "source_type": "OFFICIAL_PUBLIC_NOTICE",
                "evidence_stage": "SOURCE_MATERIAL",
                "source_document": f"{ref.lower()}.txt",
                "source_organization": "Official Public Authority (Test Ref)",
                "record_reference": ref,
                "notes": notes,
            },
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        return resp.json()

    def _create_org(self, name: str, role: str) -> tuple[dict, str]:
        resp = self.client.post(
            "/api/v1/organizations",
            headers=self.admin_headers,
            json={
                "name": name,
                "role": role,
                "connection_status": "CONNECTED",
                "source_document": "org_auth_test.json",
                "source_organization": "Registry (Test Ref)",
            },
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        body = resp.json()
        return body["organization"], body["access_token"]

    def _create_recall_and_batch(
        self, ev_id: str, mfg_org_id: str | None = None, qty: int | None = None
    ) -> tuple[dict, dict]:
        recall = self.client.post(
            "/api/v1/recalls",
            headers=self.admin_headers,
            json={
                "external_recall_number": f"EXT-{ev_id}",
                "title": "Test Recall Notice",
                "issuing_authority": "Official Authority",
                "product_description": "Test Product Scope",
                "reason": "Test Hazard",
                "manufacturer_org_id": mfg_org_id,
                "evidence_id": ev_id,
            },
        ).json()
        batch = self.client.post(
            f"/api/v1/recalls/{recall['id']}/batches",
            headers=self.admin_headers,
            json={
                "batch_code": "LOT-VAL-01",
                "product_name": "Test Product",
                "reported_quantity": qty,
                "evidence_id": ev_id,
            },
        ).json()
        return recall, batch

    def test_security_fixes_admin_write_protection_and_unconfigured_key_fail_closed(
        self,
    ) -> None:
        # Unauthenticated sensitive writes fail closed with 401
        self.assertEqual(
            self.client.post("/api/v1/evidence", json={}).status_code, 401
        )
        self.assertEqual(
            self.client.post("/api/v1/organizations", json={}).status_code, 401
        )
        self.assertEqual(
            self.client.post("/api/v1/recalls", json={}).status_code, 401
        )

        # Unconfigured ADMIN_API_KEY fails closed with 503
        no_key_app = create_app(database_path=self._db_path, admin_api_key=None)
        no_key_client = TestClient(no_key_app)
        resp = no_key_client.post(
            "/api/v1/organizations",
            headers={"X-Admin-Key": "anything"},
            json={},
        )
        self.assertEqual(resp.status_code, 503)

    def test_tool_registry_blocks_unauthorized_tool_invocations(self) -> None:
        registry = ToolRegistry()
        admin_auth = AgentAuthContext(is_admin=True, boundary=None)

        # Source Analyst cannot create verification findings
        with self.assertRaises(ToolPermissionError):
            registry.invoke(
                agent_role=AgentRole.RECALL_SOURCE_ANALYST,
                auth=admin_auth,
                tool_name="create_verification_finding",
                db_path=self._db_path,
            )

        # Coordinator cannot directly read retailer private inventory
        with self.assertRaises(ToolPermissionError):
            registry.invoke(
                agent_role=AgentRole.RECALL_RESPONSE_COORDINATOR,
                auth=admin_auth,
                tool_name="read_retailer_inventory",
                db_path=self._db_path,
                recall_id="rec_any",
            )

        # Manufacturer Ops cannot propose human approvals
        with self.assertRaises(ToolPermissionError):
            registry.invoke(
                agent_role=AgentRole.MANUFACTURER_OPERATIONS_AGENT,
                auth=admin_auth,
                tool_name="propose_human_approval",
                db_path=self._db_path,
                recall_id="rec_any",
                action_type="ANY",
                rationale="ANY",
            )

    def test_all_seven_agents_execution_boundary_enforcement_and_contradictions(
        self,
    ) -> None:
        # List all 7 agents via API
        agents_resp = self.client.get("/api/v1/agents", headers=self.admin_headers)
        self.assertEqual(agents_resp.status_code, 200)
        self.assertEqual(len(agents_resp.json()), 7)

        ev1 = self._create_evidence(
            "REF-P4-001",
            notes="Recall Number: REF-P4-001\nProduct: Test Item\nLot: LOT-VAL-01\nHazard: Validation Check\nDate: 2026-11-21",
        )
        ev2 = self._create_evidence("REF-P4-002", notes="Independent corroboration ref")
        mfg_org, mfg_token = self._create_org("Mfg Entity Test", "MANUFACTURER")
        dist_org, dist_token = self._create_org("Dist Entity Test", "DISTRIBUTOR")
        ret_org, ret_token = self._create_org("Ret Entity Test", "RETAILER")
        recall, batch = self._create_recall_and_batch(
            ev1["id"], mfg_org_id=mfg_org["id"], qty=None
        )

        # 1. Recall Source Analyst (deterministic extraction, never sets VERIFIED)
        r1 = self.client.post(
            "/api/v1/agents/RECALL_SOURCE_ANALYST/run",
            headers={**self.admin_headers, "X-Idempotency-Key": "idem-source-001"},
            json={"payload": {"evidence_id": ev1["id"]}},
        )
        self.assertEqual(r1.status_code, 200, r1.text)
        env1 = r1.json()
        self.assertEqual(env1["execution_status"], "COMPLETED")
        self.assertEqual(env1["execution_mode"], "DETERMINISTIC")
        self.assertEqual(
            env1["output"]["evidence_verification_status"], "AWAITING_EVIDENCE"
        )
        self.assertTrue(len(env1["output"]["extracted_claims"]) >= 4)

        # Idempotency check returns prior execution ID without duplicating work
        r1_dup = self.client.post(
            "/api/v1/agents/RECALL_SOURCE_ANALYST/run",
            headers={**self.admin_headers, "X-Idempotency-Key": "idem-source-001"},
            json={"payload": {"evidence_id": ev1["id"]}},
        )
        self.assertEqual(r1_dup.status_code, 200)
        self.assertTrue(r1_dup.json()["reused_idempotent_result"])
        self.assertEqual(r1_dup.json()["execution_id"], env1["execution_id"])

        # 2. Recall Scope Agent preserves NULL unknown quantity
        r2 = self.client.post(
            "/api/v1/agents/RECALL_SCOPE_AGENT/run",
            headers=self.admin_headers,
            json={"payload": {"recall_id": recall["id"]}},
        )
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["output"]["unknown_quantity_batch_ids"], [batch["id"]])
        self.assertEqual(r2.json()["output"]["scope_status"], "AWAITING_EVIDENCE")

        # 3. Manufacturer Operations Agent — wrong org role fails closed with 403
        bad_mfg = self.client.post(
            "/api/v1/agents/MANUFACTURER_OPERATIONS_AGENT/run",
            headers={"Authorization": f"Bearer {dist_token}"},
            json={"payload": {"recall_id": recall["id"]}},
        )
        self.assertEqual(bad_mfg.status_code, 403)

        # Manufacturer registers one dispatch with unknown quantity, then runs Manufacturer Agent
        self.client.post(
            "/api/v1/shipments",
            headers={"Authorization": f"Bearer {mfg_token}"},
            json={
                "recall_id": recall["id"],
                "batch_id": batch["id"],
                "to_org_id": dist_org["id"],
                "quantity_shipped": None,
                "evidence_id": ev1["id"],
            },
        )
        r3 = self.client.post(
            "/api/v1/agents/MANUFACTURER_OPERATIONS_AGENT/run",
            headers={"Authorization": f"Bearer {mfg_token}"},
            json={"payload": {"recall_id": recall["id"]}},
        )
        self.assertEqual(r3.status_code, 200, r3.text)
        self.assertEqual(len(r3.json()["output"]["dispatches"]), 1)
        self.assertEqual(r3.json()["output"]["missing_acknowledgement_org_ids"], [dist_org["id"]])

        # 4. Distributor Reconciliation Agent (empty outgoing shipments -> AWAITING_EVIDENCE)
        r4 = self.client.post(
            "/api/v1/agents/DISTRIBUTOR_RECONCILIATION_AGENT/run",
            headers={"Authorization": f"Bearer {dist_token}"},
            json={"payload": {"recall_id": recall["id"], "expected_incoming_quantity": None}},
        )
        self.assertEqual(r4.status_code, 200, r4.text)
        self.assertEqual(r4.json()["output"]["reconciliation_status"], "AWAITING_EVIDENCE")
        self.assertIsNone(r4.json()["output"]["total_known_outgoing_quantity"])

        # 5. Retailer Inventory Agent — missing records do NOT infer zero affected stock
        r5 = self.client.post(
            "/api/v1/agents/RETAILER_INVENTORY_AGENT/run",
            headers={"Authorization": f"Bearer {ret_token}"},
            json={"payload": {"recall_id": recall["id"]}},
        )
        self.assertEqual(r5.status_code, 200, r5.text)
        self.assertEqual(r5.json()["output"]["assessment_status"], "AWAITING_EVIDENCE")
        self.assertTrue(
            any("zero affected stock" in g for g in r5.json()["output"]["unresolved_gaps"])
        )

        # 6. Independent Verification Agent — detects contradiction and never silently resolves
        r6_mismatch = self.client.post(
            "/api/v1/agents/INDEPENDENT_VERIFICATION_AGENT/run",
            headers=self.admin_headers,
            json={
                "payload": {
                    "recall_id": recall["id"],
                    "primary_evidence_id": ev1["id"],
                    "corroborating_evidence_id": ev2["id"],
                    "expected_quantity": 50,
                    "reported_quantity": 40,
                }
            },
        )
        self.assertEqual(r6_mismatch.status_code, 200, r6_mismatch.text)
        out6 = r6_mismatch.json()["output"]
        self.assertEqual(out6["overall_status"], "DISCREPANCY")
        self.assertFalse(out6["policy_satisfied_for_verified"])
        self.assertTrue(len(out6["contradictions_detected"]) >= 1)

        # Independent Verification Agent — policy satisfied with distinct corroborating evidence
        r6_match = self.client.post(
            "/api/v1/agents/INDEPENDENT_VERIFICATION_AGENT/run",
            headers=self.admin_headers,
            json={
                "payload": {
                    "recall_id": recall["id"],
                    "primary_evidence_id": ev1["id"],
                    "corroborating_evidence_id": ev2["id"],
                    "expected_quantity": 50,
                    "reported_quantity": 50,
                }
            },
        )
        self.assertEqual(r6_match.status_code, 200, r6_match.text)
        self.assertEqual(r6_match.json()["output"]["overall_status"], "VERIFIED")
        self.assertTrue(r6_match.json()["output"]["policy_satisfied_for_verified"])

        # 7. Recall Response Coordinator — consolidates findings and proposes strictly PENDING approval
        r7 = self.client.post(
            "/api/v1/agents/RECALL_RESPONSE_COORDINATOR/run",
            headers=self.admin_headers,
            json={"payload": {"recall_id": recall["id"], "propose_human_review_action": True}},
        )
        self.assertEqual(r7.status_code, 200, r7.text)
        out7 = r7.json()["output"]
        self.assertEqual(out7["discrepancy_finding_count"], 1)
        self.assertEqual(out7["verified_finding_count"], 1)
        self.assertEqual(len(out7["proposed_approvals"]), 1)
        self.assertEqual(out7["proposed_approvals"][0]["decision"], "PENDING")

        # Audit log sanitization check: no bearer tokens or admin keys leaked in execution logs
        audits = self.client.get(
            "/api/v1/agents/executions", headers=self.admin_headers
        ).json()
        self.assertTrue(len(audits) >= 7)
        serialized = str(audits)
        self.assertNotIn(self.ADMIN_KEY, serialized)
        self.assertNotIn(mfg_token, serialized)
        self.assertNotIn(dist_token, serialized)
        self.assertNotIn(ret_token, serialized)

    def test_bounded_timeout_and_execution_failure_auditing(self) -> None:
        registry = ToolRegistry()
        auth = AgentAuthContext(is_admin=True, boundary=None)

        # Timeout enforcement
        slow_agent = _SlowTimeoutAgent(
            db_path=self._db_path, tool_registry=registry, timeout_seconds=0.05
        )
        timeout_env = slow_agent.run({"ref_id": "rec_timeout_probe"}, auth)
        self.assertEqual(timeout_env.execution_status, "TIMED_OUT")
        self.assertIsNone(timeout_env.output)

        # Failure handling when recall_id does not exist
        fail_resp = self.client.post(
            "/api/v1/agents/RECALL_SCOPE_AGENT/run",
            headers=self.admin_headers,
            json={"payload": {"recall_id": "rec_does_not_exist"}},
        )
        self.assertEqual(fail_resp.status_code, 200)
        self.assertEqual(fail_resp.json()["execution_status"], "FAILED")

        audits = list_execution_audits(self._db_path)
        statuses = {a.execution_status for a in audits}
        self.assertIn("TIMED_OUT", statuses)
        self.assertIn("FAILED", statuses)


if __name__ == "__main__":
    unittest.main()
