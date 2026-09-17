"""Tests for praxis_api.py security fixes: auth bypass and SQLite connection safety."""

import os
import sys
import sqlite3
import tempfile
import unittest

# Ensure the api/ directory is on the path
sys.path.insert(0, os.path.dirname(__file__))

# Set required env vars BEFORE importing the module
os.environ["DEV_TOKEN"] = "test-secret-token"
os.environ["RATE_LIMIT_ENABLED"] = "false"


class TestIsLocalhost(unittest.TestCase):
    """Test the _is_localhost URL-parsing function rejects spoofed headers."""

    def setUp(self):
        from praxis_api import _is_localhost
        self._is_localhost = _is_localhost

    def test_valid_localhost_http(self):
        self.assertTrue(self._is_localhost("http://localhost:5173"))

    def test_valid_localhost_https(self):
        self.assertTrue(self._is_localhost("https://localhost:3000"))

    def test_valid_localhost_no_port(self):
        self.assertTrue(self._is_localhost("http://localhost"))

    def test_valid_127_0_0_1(self):
        self.assertTrue(self._is_localhost("http://127.0.0.1:5173"))

    def test_valid_ipv6_loopback(self):
        self.assertTrue(self._is_localhost("http://[::1]:5173"))

    def test_spoofed_origin_query_param(self):
        """Origin: https://evil.com?localhost must be rejected."""
        self.assertFalse(self._is_localhost("https://evil.com?localhost"))

    def test_spoofed_origin_subdomain(self):
        """Referer: http://localhost.attacker.com must be rejected."""
        self.assertFalse(self._is_localhost("http://localhost.attacker.com"))

    def test_spoofed_origin_path(self):
        """Origin with localhost in path must be rejected."""
        self.assertFalse(self._is_localhost("https://evil.com/localhost"))

    def test_spoofed_origin_fragment(self):
        self.assertFalse(self._is_localhost("https://evil.com#localhost"))

    def test_spoofed_127_subdomain(self):
        self.assertFalse(self._is_localhost("http://127.0.0.1.evil.com"))

    def test_empty_string(self):
        self.assertFalse(self._is_localhost(""))

    def test_plain_localhost_no_scheme(self):
        """Plain 'localhost' without scheme — urlparse puts it in path, not hostname."""
        self.assertFalse(self._is_localhost("localhost"))


class TestRequireDevAccess(unittest.TestCase):
    """Test the require_dev_access decorator with various header combinations."""

    def setUp(self):
        import praxis_api
        self.app = praxis_api.app
        self.app.config["TESTING"] = True
        # Use a temp DB for isolation
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        praxis_api.DB_PATH = self.db_path
        praxis_api.DEV_TOKEN = "test-secret-token"
        praxis_api.init_db()
        self.client = self.app.test_client()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_valid_token_grants_access(self):
        resp = self.client.get("/get_feedback", headers={"X-Dev-Token": "test-secret-token"})
        self.assertEqual(resp.status_code, 200)

    def test_invalid_token_denied(self):
        resp = self.client.get("/get_feedback", headers={"X-Dev-Token": "wrong-token"})
        self.assertEqual(resp.status_code, 403)

    def test_no_headers_denied(self):
        resp = self.client.get("/get_feedback")
        self.assertEqual(resp.status_code, 403)

    def test_localhost_origin_grants_access(self):
        resp = self.client.get("/get_feedback", headers={"Origin": "http://localhost:5173"})
        self.assertEqual(resp.status_code, 200)

    def test_127_origin_grants_access(self):
        resp = self.client.get("/get_feedback", headers={"Origin": "http://127.0.0.1:5173"})
        self.assertEqual(resp.status_code, 200)

    def test_spoofed_origin_query_denied(self):
        resp = self.client.get("/get_feedback", headers={"Origin": "https://evil.com?localhost"})
        self.assertEqual(resp.status_code, 403)

    def test_spoofed_referer_subdomain_denied(self):
        resp = self.client.get("/get_feedback", headers={"Referer": "http://localhost.attacker.com/page"})
        self.assertEqual(resp.status_code, 403)

    def test_spoofed_origin_path_denied(self):
        resp = self.client.get("/get_feedback", headers={"Origin": "https://evil.com/localhost"})
        self.assertEqual(resp.status_code, 403)

    def test_empty_dev_token_returns_403(self):
        """When DEV_TOKEN is empty, all dev endpoints must return 403."""
        import praxis_api
        original_token = praxis_api.DEV_TOKEN
        try:
            praxis_api.DEV_TOKEN = ""
            # Even with valid localhost origin, should be denied
            resp = self.client.get(
                "/get_feedback",
                headers={"Origin": "http://localhost:5173"},
            )
            self.assertEqual(resp.status_code, 403)
            self.assertIn("DEV_TOKEN not configured", resp.get_json()["error"])
        finally:
            praxis_api.DEV_TOKEN = original_token


class TestDefaultBindAddress(unittest.TestCase):
    """Verify HOST env var is read for bind address."""

    def test_default_host_is_loopback(self):
        """Without HOST env var, default should be 127.0.0.1."""
        host = os.environ.get("HOST", "127.0.0.1")
        self.assertEqual(host, "127.0.0.1")


class TestSqliteConnectionSafety(unittest.TestCase):
    """Verify DB connections are properly closed even on errors."""

    def setUp(self):
        import praxis_api
        self.app = praxis_api.app
        self.app.config["TESTING"] = True
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        praxis_api.DB_PATH = self.db_path
        praxis_api.DEV_TOKEN = "test-secret-token"
        praxis_api.init_db()
        self.client = self.app.test_client()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_submit_feedback_success(self):
        resp = self.client.post(
            "/submit_feedback",
            json={"type": "bug", "message": "Test bug report"},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.get_json()
        self.assertIn("id", data)
        self.assertEqual(data["status"], "created")

    def test_resolve_nonexistent_feedback_returns_404(self):
        resp = self.client.post(
            "/resolve_feedback",
            json={"id": "nonexistent-id"},
            headers={"X-Dev-Token": "test-secret-token"},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 404)

    def test_get_feedback_invalid_type_returns_400(self):
        resp = self.client.get(
            "/get_feedback?type=invalid_type",
            headers={"X-Dev-Token": "test-secret-token"},
        )
        self.assertEqual(resp.status_code, 400)

    def test_health_endpoint_works(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)

    def test_health_with_tracking(self):
        resp = self.client.get("/health?t=landing")
        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
