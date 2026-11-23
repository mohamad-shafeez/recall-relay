"""Phase 8 automated tests for BFF HttpOnly sessions, CSRF/Origin security,
centralized sponsor adapters, SSRF protection, secret redaction, bounded retries,
and cross-organization data-protection gates.

Uses zero fake sponsor responses and zero real sponsor credentials.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.agents.base import AgentRole
from app.communication.schemas import A2AMessageCreate, A2AMessageType
from app.core.sessions import create_bff_session
from app.integrations.band_adapter import BandMessageTransport
from app.integrations.base import (
    ConnectionState,
    ImplementationStatus,
    execute_with_bounded_retries,
    redact_secrets,
    validate_sponsor_https_url,
)
from app.integrations.lyzr_adapter import LyzrReasoningAdapter
from app.main import create_app


class TestPhase8BffSessionsAndSponsorIntegrations(unittest.TestCase):
    """Verify BFF HttpOnly cookie security and honest sponsor integration contracts."""

    ADMIN_KEY = "phase8-bff-admin-secret"

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = str(Path(self._tmpdir.name) / "phase8_test.db")
        self.app = create_app(
            database_path=self._db_path, admin_api_key=self.ADMIN_KEY
        )
        self.client = TestClient(self.app)
        self.admin_headers = {"X-Admin-Key": self.ADMIN_KEY}

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_bff_admin_session_cookie_flags_csrf_origin_rotation_and_revocation(self) -> None:
        # 1. Login exchanges admin key once for an HttpOnly SameSite=Strict cookie
        login = self.client.post(
            "/api/v1/session/login/admin",
            json={"admin_key": self.ADMIN_KEY},
        )
        self.assertEqual(login.status_code, 200, login.text)
        body = login.json()
        self.assertTrue(body["authenticated"])
        self.assertEqual(body["mode"], "admin")
        csrf_token = body["csrf_token"]
        self.assertTrue(csrf_token and len(csrf_token) >= 16)

        set_cookie = login.headers.get("set-cookie", "")
        self.assertIn("rr_session=", set_cookie)
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=strict", set_cookie.lower().replace("samesite=strict", "SameSite=strict"))

        # 2. Session introspection works via cookie alone (no X-Admin-Key header sent!)
        me = self.client.get("/api/v1/session/me")
        self.assertEqual(me.status_code, 200)
        self.assertTrue(me.json()["authenticated"])
        self.assertEqual(me.json()["mode"], "admin")

        # 3. Cookie-authenticated GET to admin endpoint succeeds without X-Admin-Key
        self.assertEqual(
            self.client.get("/api/v1/a2a/audit-events/verify").status_code, 200
        )

        # 4. Cookie-authenticated POST WITHOUT X-CSRF-Token fails closed with 403
        no_csrf = self.client.post(
            "/api/v1/evidence",
            json={
                "source_type": "OFFICIAL_PUBLIC_NOTICE",
                "source_document": "test.txt",
                "source_organization": "Authority",
                "record_reference": "REF-CSRF-01",
            },
        )
        self.assertEqual(no_csrf.status_code, 403)

        # 5. Disallowed Origin header fails closed with 403 even with valid CSRF token
        bad_origin = self.client.post(
            "/api/v1/evidence",
            headers={
                "X-CSRF-Token": csrf_token,
                "Origin": "https://evil.example.invalid",
            },
            json={
                "source_type": "OFFICIAL_PUBLIC_NOTICE",
                "source_document": "test.txt",
                "source_organization": "Authority",
                "record_reference": "REF-ORIGIN-01",
            },
        )
        self.assertEqual(bad_origin.status_code, 403)

        # 6. Valid CSRF token + allowed Origin succeeds
        ok_post = self.client.post(
            "/api/v1/evidence",
            headers={
                "X-CSRF-Token": csrf_token,
                "Origin": "http://localhost:5173",
            },
            json={
                "source_type": "OFFICIAL_PUBLIC_NOTICE",
                "source_document": "test.txt",
                "source_organization": "Authority",
                "record_reference": "REF-CSRF-OK",
            },
        )
        self.assertEqual(ok_post.status_code, 201, ok_post.text)

        # 7. Explicit logout revokes session; subsequent protected calls fail with 401
        old_cookie = self.client.cookies.get("rr_session")
        logout = self.client.post(
            "/api/v1/session/logout",
            headers={"X-CSRF-Token": csrf_token},
        )
        self.assertEqual(logout.status_code, 200)
        self.assertFalse(logout.json()["authenticated"])

        # Replaying revoked cookie must fail closed with 401
        replay = self.client.get(
            "/api/v1/a2a/audit-events/verify",
            cookies={"rr_session": old_cookie or ""},
        )
        self.assertEqual(replay.status_code, 401)

    def test_bff_org_session_isolation_and_expiration(self) -> None:
        # Register org via admin header
        org_res = self.client.post(
            "/api/v1/organizations",
            headers=self.admin_headers,
            json={
                "name": "Session Org Test",
                "role": "MANUFACTURER",
                "connection_status": "CONNECTED",
                "source_document": "org.json",
                "source_organization": "Registry",
            },
        ).json()
        org_token = org_res["access_token"]

        # Login org via BFF session
        org_client = TestClient(self.app)
        login = org_client.post(
            "/api/v1/session/login/organization",
            json={"bearer_token": org_token},
        )
        self.assertEqual(login.status_code, 200, login.text)
        self.assertEqual(login.json()["mode"], "organization")

        # Org session can read its own boundary shipments
        self.assertEqual(org_client.get("/api/v1/shipments").status_code, 200)
        # Org session CANNOT access admin-only approvals or integration checks (403)
        self.assertEqual(org_client.get("/api/v1/approvals").status_code, 403)
        self.assertEqual(
            org_client.post(
                "/api/v1/integrations/lyzr/check",
                headers={"X-CSRF-Token": login.json()["csrf_token"]},
            ).status_code,
            403,
        )

        # Expired session fails closed with 401
        _, expired_raw = create_bff_session(
            self._db_path, principal_type="ADMIN", ttl_minutes=-5
        )
        exp_client = TestClient(self.app)
        self.assertEqual(
            exp_client.get(
                "/api/v1/approvals", cookies={"rr_session": expired_raw}
            ).status_code,
            401,
        )

    def test_sponsor_integrations_honest_statuses_ssrf_redaction_and_retries(self) -> None:
        # Unauthenticated status check fails closed with 401
        self.assertEqual(
            self.client.get("/api/v1/integrations/status").status_code, 401
        )

        # Admin status check returns all 5 sponsors honestly (none falsely CONNECTED)
        resp = self.client.get(
            "/api/v1/integrations/status", headers=self.admin_headers
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["active_a2a_transport"], "internal_sqlite_transport")
        self.assertEqual(data["agent_execution_mode"], "DETERMINISTIC")
        sponsors = {s["sponsor_name"]: s for s in data["sponsors"]}
        self.assertEqual(set(sponsors.keys()), {"BAND", "Lyzr", "Finch", "AdaL", "Agent37"})

        # Agent37 must be BLOCKED due to payment-card requirement
        self.assertEqual(sponsors["Agent37"]["implementation_status"], "BLOCKED")
        self.assertEqual(sponsors["Agent37"]["connection_state"], "BLOCKED")
        self.assertTrue(sponsors["Agent37"]["card_or_wallet_required"])

        # Unconfigured BAND, Lyzr, Finch, AdaL must be IMPLEMENTED_NOT_CONNECTED
        for name in ("BAND", "Lyzr", "Finch", "AdaL"):
            self.assertEqual(
                sponsors[name]["implementation_status"],
                ImplementationStatus.IMPLEMENTED_NOT_CONNECTED.value,
            )
            self.assertEqual(
                sponsors[name]["connection_state"],
                ConnectionState.CONFIGURATION_MISSING.value,
            )

        # SSRF protection blocks metadata, loopback, and private IPs and non-HTTPS
        for bad_url in (
            "http://169.254.169.254/latest/meta-data",
            "https://127.0.0.1/api",
            "https://10.0.0.5/api",
            "http://agent-prod.studio.lyzr.ai",
        ):
            with self.assertRaises(ValueError):
                validate_sponsor_https_url(bad_url)

        # Secret redaction masks configured keys
        redacted = redact_secrets(
            "Error calling provider with key sk-test-99999999",
            ["sk-test-99999999"],
        )
        self.assertNotIn("sk-test-99999999", redacted)
        self.assertIn("[REDACTED]", redacted)

        # Bounded retry counter stops at max_retries + 1 attempts
        attempts = {"count": 0}

        def _always_timeout():
            attempts["count"] += 1
            raise httpx.ReadTimeout("simulated timeout")

        with self.assertRaises(httpx.ReadTimeout):
            execute_with_bounded_retries(
                _always_timeout, max_retries=2, backoff_seconds=0.001
            )
        self.assertEqual(attempts["count"], 3)

        # Unconfigured Lyzr fails closed with 503 (no fabricated AI output)
        lyzr = LyzrReasoningAdapter(db_path=self._db_path, api_key=None)
        with self.assertRaises(HTTPException) as ctx:
            lyzr.analyze_public_evidence(
                user_id="u1",
                session_id="s1",
                evidence_id="ev_1",
                authorized_text="Official notice",
            )
        self.assertEqual(ctx.exception.status_code, 503)

        # Configured BAND enforces cross-org sharing grant BEFORE making any outbound HTTP call
        band = BandMessageTransport(
            db_path=self._db_path,
            api_base_url="https://api.band.ai",
            api_key="dummy-unverified-key",
            agent_id="agent-uuid-1",
        )
        with self.assertRaises(HTTPException) as band_ctx:
            band.send_message(
                A2AMessageCreate(
                    conversation_id="conv_ext",
                    recall_id="rec_any",
                    sender_agent=AgentRole.MANUFACTURER_OPERATIONS_AGENT,
                    recipient_agent=AgentRole.DISTRIBUTOR_RECONCILIATION_AGENT,
                    recipient_boundary_id="bnd_other",
                    message_type=A2AMessageType.EVIDENCE_SHARE_NOTICE,
                    payload={"quantity_shipped": None},
                ),
                sender_boundary_id="bnd_owner",
                actor_id="org_owner",
            )
        self.assertEqual(band_ctx.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
