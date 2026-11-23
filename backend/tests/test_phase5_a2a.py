"""Phase 5 automated tests for durable A2A messaging, state machine, concurrency,
task dependencies, real agent handoffs, cross-org sharing/revocation, governance,
cooperative cancellation safety, scoped idempotency, public/private isolation,
and hash-chained audit trail integrity.

Uses abstract structural validation payloads only; no fabricated business records.
"""

from __future__ import annotations

import threading
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.agents.base import AgentAuthContext, AgentRole, BaseAgent
from app.agents.tools import ToolRegistry
from app.communication.audit_trail import verify_audit_chain
from app.communication.schemas import A2AMessageCreate, A2AMessageType
from app.communication.sharing_service import check_sharing_authorization
from app.communication.task_service import add_task_dependency
from app.communication.transport import InternalSQLiteTransport
from app.main import create_app


class _CancelProbeInput(BaseModel):
    ref_id: str


class _CancelProbeOutput(BaseModel):
    completed: bool


class _CooperativeCancelAgent(BaseAgent[_CancelProbeInput, _CancelProbeOutput]):
    """Proves a timed-out agent stops cooperatively BEFORE performing a side effect."""

    agent_id = "agent_cancel_probe"
    role = AgentRole.RECALL_SCOPE_AGENT
    input_schema = _CancelProbeInput
    output_schema = _CancelProbeOutput

    def __init__(self, *args, side_effect_flag: list[bool], **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.side_effect_flag = side_effect_flag

    def extract_input_references(self, validated_input: _CancelProbeInput) -> List[str]:
        return [validated_input.ref_id]

    def extract_output_and_evidence_references(
        self, validated_output: _CancelProbeOutput
    ) -> tuple[List[str], List[str]]:
        return [], []

    def execute(
        self, validated_input: _CancelProbeInput, auth: AgentAuthContext
    ) -> _CancelProbeOutput:
        time.sleep(0.12)
        # Cooperative checkpoint (also enforced inside invoke_tool)
        self.check_cancelled()
        # Must NEVER execute after timeout
        self.side_effect_flag.append(True)
        return _CancelProbeOutput(completed=True)


class TestPhase5A2ACoordination(unittest.TestCase):
    """Comprehensive Phase 5 tests covering A2A transport, tasks, sharing, governance, and safety."""

    ADMIN_KEY = "test-admin-secret-phase5"

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = str(Path(self._tmpdir.name) / "phase5_test.db")
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
                "source_organization": "Official Authority (Test Ref)",
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
                "source_document": "org_ref.json",
                "source_organization": "Registry (Test Ref)",
            },
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        data = resp.json()
        return data["organization"], data["access_token"]

    def _create_recall(self, ev_id: str) -> dict:
        resp = self.client.post(
            "/api/v1/recalls",
            headers=self.admin_headers,
            json={
                "external_recall_number": f"EXT-{ev_id}",
                "title": "Phase 5 Coordination Test Notice",
                "issuing_authority": "Official Authority",
                "product_description": "Test Scope Item",
                "reason": "Test Hazard",
                "evidence_id": ev_id,
            },
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        return resp.json()

    def test_message_lifecycle_idempotency_expiration_and_concurrent_claiming(self) -> None:
        ev = self._create_evidence("REF-MSG-001")
        recall = self._create_recall(ev["id"])
        transport = InternalSQLiteTransport(self._db_path)

        # Create + duplicate idempotency returns same message ID
        m1 = transport.send_message(
            A2AMessageCreate(
                conversation_id="conv_001",
                recall_id=recall["id"],
                sender_agent=AgentRole.RECALL_SOURCE_ANALYST,
                recipient_agent=AgentRole.RECALL_SCOPE_AGENT,
                message_type=A2AMessageType.TASK_HANDOFF,
                payload={"recall_id": recall["id"]},
                evidence_references=[ev["id"]],
                idempotency_key="msg-idem-001",
                max_attempts=2,
            ),
            sender_boundary_id=None,
            actor_id="admin",
        )
        m1_dup = transport.send_message(
            A2AMessageCreate(
                conversation_id="conv_001",
                recall_id=recall["id"],
                sender_agent=AgentRole.RECALL_SOURCE_ANALYST,
                recipient_agent=AgentRole.RECALL_SCOPE_AGENT,
                message_type=A2AMessageType.TASK_HANDOFF,
                payload={"recall_id": recall["id"]},
                evidence_references=[ev["id"]],
                idempotency_key="msg-idem-001",
            ),
            sender_boundary_id=None,
            actor_id="admin",
        )
        self.assertEqual(m1.id, m1_dup.id)
        self.assertEqual(m1.status.value, "PENDING")

        # Deliver and acknowledge
        delivered = transport.deliver_message(m1.id, actor_id="admin")
        self.assertEqual(delivered.status.value, "DELIVERED")
        acked = transport.acknowledge_message(m1.id, actor_id="admin", caller_boundary_id=None)
        self.assertIsNotNone(acked.acknowledged_at)

        # Concurrent claiming across 5 threads: EXACTLY 1 thread must claim the message
        barrier = threading.Barrier(5)

        def _claim_worker(idx: int):
            barrier.wait()
            return transport.claim_next_message(
                AgentRole.RECALL_SCOPE_AGENT, worker_id=f"worker_{idx}"
            )

        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(_claim_worker, range(5)))

        claimed = [r for r in results if r is not None]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].id, m1.id)
        self.assertEqual(claimed[0].attempt_count, 1)

        # Retry bound enforcement (max_attempts=2)
        retry_1 = transport.mark_failed(m1.id, actor_id="worker_0", error_message="transient")
        self.assertEqual(retry_1.status.value, "PENDING")
        transport.deliver_message(m1.id, actor_id="admin")
        transport.claim_next_message(AgentRole.RECALL_SCOPE_AGENT, worker_id="worker_1")
        terminal_fail = transport.mark_failed(m1.id, actor_id="worker_1", error_message="exhausted")
        self.assertEqual(terminal_fail.status.value, "FAILED")

        # Expired message transitions to EXPIRED automatically
        past_iso = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        expired_msg = transport.send_message(
            A2AMessageCreate(
                conversation_id="conv_exp",
                recall_id=recall["id"],
                sender_agent=AgentRole.RECALL_SOURCE_ANALYST,
                recipient_agent=AgentRole.RECALL_SCOPE_AGENT,
                message_type=A2AMessageType.TASK_HANDOFF,
                payload={"recall_id": recall["id"]},
                expires_at=past_iso,
            ),
            sender_boundary_id=None,
            actor_id="admin",
        )
        self.assertEqual(expired_msg.status.value, "EXPIRED")

    def test_real_a2a_handoff_and_task_dependency_cycle_protection(self) -> None:
        ev = self._create_evidence(
            "REF-HANDOFF-001",
            notes="Recall Number: REF-HANDOFF-001\nProduct: Scope Check\nLot: LOT-A2A-1",
        )
        recall = self._create_recall(ev["id"])

        # Dispatch TASK_HANDOFF from Source Analyst to Recall Scope Agent
        handoff_resp = self.client.post(
            "/api/v1/a2a/handoffs",
            headers=self.admin_headers,
            json={
                "recall_id": recall["id"],
                "conversation_id": "conv_handoff_01",
                "sender_agent": "RECALL_SOURCE_ANALYST",
                "recipient_agent": "RECALL_SCOPE_AGENT",
                "requested_action": "evaluate_recall_scope",
                "required_output_schema": "RecallScopeOutput",
                "payload": {"recall_id": recall["id"]},
                "evidence_references": [ev["id"]],
            },
        )
        self.assertEqual(handoff_resp.status_code, 201, handoff_resp.text)
        task_id = handoff_resp.json()["task"]["id"]

        # Process Recall Scope Agent inbox -> executes specialist agent and publishes RESULT_REPORT
        proc_resp = self.client.post(
            "/api/v1/a2a/inbox/RECALL_SCOPE_AGENT/process",
            headers=self.admin_headers,
        )
        self.assertEqual(proc_resp.status_code, 200, proc_resp.text)
        proc_body = proc_resp.json()
        self.assertTrue(proc_body["processed"])
        self.assertEqual(proc_body["processed_message"]["status"], "PROCESSED")
        self.assertEqual(proc_body["result_message"]["message_type"], "RESULT_REPORT")

        # Verify task transitioned to COMPLETED with execution reference
        task_after = self.client.get(
            f"/api/v1/a2a/tasks/{task_id}", headers=self.admin_headers
        ).json()
        self.assertEqual(task_after["status"], "COMPLETED")
        self.assertIsNotNone(task_after["result_reference"])

        # Blocked missing auth when dispatching org-scoped agent without recipient_boundary_id
        blocked_auth = self.client.post(
            "/api/v1/a2a/handoffs",
            headers=self.admin_headers,
            json={
                "recall_id": recall["id"],
                "conversation_id": "conv_handoff_02",
                "sender_agent": "RECALL_RESPONSE_COORDINATOR",
                "recipient_agent": "RETAILER_INVENTORY_AGENT",
                "requested_action": "inspect_inventory",
                "required_output_schema": "RetailerInventoryOutput",
                "payload": {"recall_id": recall["id"]},
            },
        )
        self.assertEqual(blocked_auth.status_code, 201)
        self.assertEqual(blocked_auth.json()["task"]["status"], "BLOCKED_MISSING_AUTH")

        # Circular dependency detection
        t_a = self.client.post(
            "/api/v1/a2a/tasks",
            headers=self.admin_headers,
            json={
                "recall_id": recall["id"],
                "conversation_id": "conv_dep",
                "assigned_agent": "RECALL_SCOPE_AGENT",
                "requested_action": "step_a",
                "required_output_schema": "RecallScopeOutput",
            },
        ).json()
        t_b = self.client.post(
            "/api/v1/a2a/tasks",
            headers=self.admin_headers,
            json={
                "recall_id": recall["id"],
                "conversation_id": "conv_dep",
                "assigned_agent": "RECALL_RESPONSE_COORDINATOR",
                "requested_action": "step_b",
                "required_output_schema": "CoordinatorOutput",
                "depends_on_task_ids": [t_a["id"]],
            },
        ).json()
        with self.assertRaises(Exception):
            add_task_dependency(self._db_path, t_a["id"], t_b["id"])

    def test_cross_org_sharing_default_deny_grant_and_revocation(self) -> None:
        ev = self._create_evidence("REF-SHARE-001")
        recall = self._create_recall(ev["id"])
        org_a, token_a = self._create_org("Owner Org Test", "MANUFACTURER")
        org_b, token_b = self._create_org("Recipient Org Test", "DISTRIBUTOR")
        headers_a = {"Authorization": f"Bearer {token_a}"}
        headers_b = {"Authorization": f"Bearer {token_b}"}

        # Default DENY before any grant exists
        allowed, _ = check_sharing_authorization(
            self._db_path,
            recall_id=recall["id"],
            owner_boundary_id=org_a["data_boundary_id"],
            recipient_boundary_id=org_b["data_boundary_id"],
            requested_fields=["quantity_shipped"],
        )
        self.assertFalse(allowed)

        # Owner grants limited fields + specific evidence ID
        grant_resp = self.client.post(
            "/api/v1/sharing/grants",
            headers=headers_a,
            json={
                "recall_id": recall["id"],
                "recipient_org_id": org_b["id"],
                "allowed_fields": ["quantity_shipped", "batch_code"],
                "allowed_evidence_ids": [ev["id"]],
                "purpose": "Downstream recall reconciliation",
                "authorized_by": "Compliance Officer A",
            },
        )
        self.assertEqual(grant_resp.status_code, 201, grant_resp.text)
        grant_id = grant_resp.json()["id"]

        # Permitted field allowed; unpermitted field denied
        ok_allowed, _ = check_sharing_authorization(
            self._db_path,
            recall_id=recall["id"],
            owner_boundary_id=org_a["data_boundary_id"],
            recipient_boundary_id=org_b["data_boundary_id"],
            requested_fields=["quantity_shipped"],
            requested_evidence_id=ev["id"],
        )
        self.assertTrue(ok_allowed)

        unpermitted, _ = check_sharing_authorization(
            self._db_path,
            recall_id=recall["id"],
            owner_boundary_id=org_a["data_boundary_id"],
            recipient_boundary_id=org_b["data_boundary_id"],
            requested_fields=["private_cost_data"],
        )
        self.assertFalse(unpermitted)

        # Non-owner cannot revoke (403)
        self.assertEqual(
            self.client.post(
                f"/api/v1/sharing/grants/{grant_id}/revoke", headers=headers_b
            ).status_code,
            403,
        )

        # Owner revokes -> immediately denied
        rev_resp = self.client.post(
            f"/api/v1/sharing/grants/{grant_id}/revoke", headers=headers_a
        )
        self.assertEqual(rev_resp.status_code, 200)
        self.assertEqual(rev_resp.json()["status"], "REVOKED")

        after_revoke, _ = check_sharing_authorization(
            self._db_path,
            recall_id=recall["id"],
            owner_boundary_id=org_a["data_boundary_id"],
            recipient_boundary_id=org_b["data_boundary_id"],
            requested_fields=["quantity_shipped"],
        )
        self.assertFalse(after_revoke)

    def test_human_approval_self_approval_prevention_and_org_identity_review(self) -> None:
        ev = self._create_evidence("REF-GOV-001")
        recall = self._create_recall(ev["id"])
        org, org_token = self._create_org("Org Identity Test", "MANUFACTURER")

        # Coordinator proposes PENDING human approval
        coord = self.client.post(
            "/api/v1/agents/RECALL_RESPONSE_COORDINATOR/run",
            headers=self.admin_headers,
            json={"payload": {"recall_id": recall["id"], "propose_human_review_action": True}},
        ).json()
        approval_id = coord["output"]["proposed_approvals"][0]["id"]

        # Unauthenticated or non-admin org token cannot decide approvals (401)
        self.assertEqual(
            self.client.post(
                f"/api/v1/approvals/{approval_id}/decision",
                headers={"Authorization": f"Bearer {org_token}"},
                json={"decision": "APPROVED", "reviewer_name": "Human", "rationale": "Test"},
            ).status_code,
            401,
        )

        # Proposing agent / any agent_* reviewer blocked with 403
        self.assertEqual(
            self.client.post(
                f"/api/v1/approvals/{approval_id}/decision",
                headers=self.admin_headers,
                json={
                    "decision": "APPROVED",
                    "reviewer_name": "agent_response_coordinator",
                    "rationale": "Self approval attempt",
                },
            ).status_code,
            403,
        )

        # Authorized human admin approves
        dec = self.client.post(
            f"/api/v1/approvals/{approval_id}/decision",
            headers=self.admin_headers,
            json={
                "decision": "APPROVED",
                "reviewer_name": "Authorized Safety Lead",
                "rationale": "Reviewed evidence references",
            },
        )
        self.assertEqual(dec.status_code, 200, dec.text)
        self.assertEqual(dec.json()["decision"], "APPROVED")

        # Organization identity review: VERIFIED without supporting_evidence_id fails (400)
        self.assertEqual(
            self.client.post(
                f"/api/v1/organizations/{org['id']}/identity-review",
                headers=self.admin_headers,
                json={"target_status": "VERIFIED", "reviewer_name": "Admin Reviewer"},
            ).status_code,
            400,
        )

        # Transition UNVERIFIED -> PENDING_VERIFICATION -> VERIFIED with evidence
        p_resp = self.client.post(
            f"/api/v1/organizations/{org['id']}/identity-review",
            headers=self.admin_headers,
            json={
                "target_status": "PENDING_VERIFICATION",
                "reviewer_name": "Admin Reviewer",
            },
        )
        self.assertEqual(p_resp.status_code, 200)
        self.assertEqual(p_resp.json()["identity_verification_status"], "PENDING_VERIFICATION")

        v_resp = self.client.post(
            f"/api/v1/organizations/{org['id']}/identity-review",
            headers=self.admin_headers,
            json={
                "target_status": "VERIFIED",
                "reviewer_name": "Admin Reviewer",
                "supporting_evidence_id": ev["id"],
            },
        )
        self.assertEqual(v_resp.status_code, 200)
        self.assertEqual(v_resp.json()["identity_verification_status"], "VERIFIED")

    def test_public_private_recall_isolation_scoped_idempotency_cancellation_and_audit_chain(
        self,
    ) -> None:
        ev = self._create_evidence("REF-PUB-001")
        recall = self._create_recall(ev["id"])

        # Unapproved recall is NOT visible publicly
        self.assertEqual(self.client.get("/api/v1/recalls").json(), [])
        self.assertEqual(
            self.client.get(f"/api/v1/recalls/{recall['id']}").status_code, 404
        )
        # Authenticated admin CAN see it
        self.assertEqual(
            self.client.get(
                f"/api/v1/recalls/{recall['id']}", headers=self.admin_headers
            ).status_code,
            200,
        )

        # After explicit public disclosure approval, it IS visible publicly
        pub = self.client.post(
            f"/api/v1/recalls/{recall['id']}/public-disclosure?approved=true",
            headers=self.admin_headers,
        )
        self.assertEqual(pub.status_code, 200)
        self.assertEqual(len(self.client.get("/api/v1/recalls").json()), 1)

        # Scoped idempotency isolation across two distinct retailer boundaries using SAME key
        _, tok_r1 = self._create_org("Retailer One", "RETAILER")
        _, tok_r2 = self._create_org("Retailer Two", "RETAILER")
        exec_r1 = self.client.post(
            "/api/v1/agents/RETAILER_INVENTORY_AGENT/run",
            headers={"Authorization": f"Bearer {tok_r1}", "X-Idempotency-Key": "shared-key-1"},
            json={"payload": {"recall_id": recall["id"]}},
        ).json()
        exec_r2 = self.client.post(
            "/api/v1/agents/RETAILER_INVENTORY_AGENT/run",
            headers={"Authorization": f"Bearer {tok_r2}", "X-Idempotency-Key": "shared-key-1"},
            json={"payload": {"recall_id": recall["id"]}},
        ).json()
        self.assertFalse(exec_r2["reused_idempotent_result"])
        self.assertNotEqual(exec_r1["execution_id"], exec_r2["execution_id"])

        # Cooperative cancellation prevents post-timeout side effect
        side_effects: list[bool] = []
        cancel_agent = _CooperativeCancelAgent(
            db_path=self._db_path,
            tool_registry=ToolRegistry(),
            timeout_seconds=0.04,
            side_effect_flag=side_effects,
        )
        env = cancel_agent.run(
            {"ref_id": recall["id"]}, AgentAuthContext(is_admin=True)
        )
        self.assertEqual(env.execution_status, "TIMED_OUT")
        time.sleep(0.15)  # Give background thread time to wake up and hit checkpoint
        self.assertEqual(side_effects, [], "Timed-out agent must not perform post-timeout side effects")

        # Hash-chained audit trail integrity verification
        chain = self.client.get(
            "/api/v1/a2a/audit-events/verify", headers=self.admin_headers
        ).json()
        self.assertTrue(chain["valid"])
        self.assertGreater(chain["event_count"], 0)
        self.assertTrue(verify_audit_chain(self._db_path).valid)


if __name__ == "__main__":
    unittest.main()
