"""Tests for the health endpoint and structured error handling."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app


class TestHealthEndpoint(unittest.TestCase):
    """Verify GET /api/health contract, DB health, and structured error handling."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = str(Path(self._tmpdir.name) / "test_health.db")
        self.app = create_app(database_path=self._db_path)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_health_endpoint_returns_200_ok(self) -> None:
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertIn("application/json", response.headers.get("content-type", ""))

    def test_health_endpoint_returns_structured_json(self) -> None:
        response = self.client.get("/api/health")
        payload = response.json()

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["service"], "RecallRelay API")
        self.assertEqual(payload["version"], "1.0.0")
        self.assertIsInstance(payload["environment"], str)
        self.assertIsInstance(payload["demo_mode"], bool)
        self.assertIsInstance(payload["uptime_seconds"], (int, float))
        self.assertGreaterEqual(payload["uptime_seconds"], 0.0)
        self.assertEqual(payload["checks"], {"api": "healthy", "database": "healthy"})

        parsed_timestamp = datetime.fromisoformat(payload["timestamp"])
        self.assertIsNotNone(parsed_timestamp.tzinfo)

    def test_unknown_route_returns_structured_404_error(self) -> None:
        response = self.client.get("/api/nonexistent-route")
        self.assertEqual(response.status_code, 404)

        payload = response.json()
        self.assertIn("error", payload)
        self.assertEqual(payload["error"]["code"], "http_error")
        self.assertEqual(payload["error"]["status_code"], 404)
        self.assertIsInstance(payload["error"]["message"], str)
        self.assertIsInstance(payload["error"]["timestamp"], str)


if __name__ == "__main__":
    unittest.main()
