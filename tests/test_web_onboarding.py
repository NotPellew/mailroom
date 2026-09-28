"""End-to-end tests for first-launch onboarding wizard and system health dashboard."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from Mailroom import app as app_module
from Mailroom.config import Config


class TestWebOnboardingAndHealth(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmpdir.name)
        self.config_path = str(self.tmp_path / "config.json")
        self.config = Config(self.config_path)
        self.config.save()

        self.app = app_module.create_app(self.config)
        self.client = self.app.test_client()
        self.csrf = self.app.config["CSRF_TOKEN"]

    def tearDown(self):
        self.tmpdir.cleanup()

    def headers(self, csrf=True, origin="http://127.0.0.1:5000", host=None):
        h = {}
        if origin is not None:
            h["Origin"] = origin
        if csrf:
            h["X-CSRF-Token"] = self.csrf
        if host is not None:
            h["Host"] = host
        return h

    # --- System Health Dashboard Tests ---

    @patch("Mailroom.classification.probe_model_endpoint", autospec=True)
    def test_system_health_success_ready(self, mock_probe):
        mock_probe.return_value = ["qwen2.5:7b"]

        res = self.client.get("/api/system/health", headers=self.headers())
        self.assertEqual(res.status_code, 200)
        data = res.get_json()

        self.assertIn("database", data)
        self.assertIn("ai", data)
        self.assertIn("gmail", data)
        self.assertIn("onboarding", data)

        # Database initial state
        self.assertIn(data["database"]["status"], ("ready", "not_initialized"))
        self.assertEqual(data["database"]["messages"], 0)
        self.assertEqual(data["database"]["decisions"], 0)

        # AI state
        self.assertEqual(data["ai"]["status"], "ready")
        self.assertEqual(data["ai"]["model"], "qwen2.5:7b")
        self.assertIn("qwen2.5:7b", data["ai"]["available_models"])

        # Gmail state (default local-only)
        self.assertIn(data["gmail"]["status"], ("local_only", "connected"))

        # Onboarding state
        self.assertFalse(data["onboarding"]["completed"])

    @patch("Mailroom.classification.probe_model_endpoint", autospec=True)
    def test_system_health_ai_action_needed(self, mock_probe):
        mock_probe.return_value = ["llama3.2:3b"]

        res = self.client.get("/api/system/health", headers=self.headers())
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["ai"]["status"], "action_needed")
        self.assertEqual(data["ai"]["model"], "qwen2.5:7b")
        self.assertNotIn("qwen2.5:7b", data["ai"]["available_models"])

    @patch("Mailroom.classification.probe_model_endpoint", autospec=True)
    def test_system_health_ai_offline(self, mock_probe):
        from Mailroom.classification import ClassificationError
        mock_probe.side_effect = ClassificationError("Could not reach Ollama endpoint")

        res = self.client.get("/api/system/health", headers=self.headers())
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["ai"]["status"], "offline")
        self.assertIn("Could not reach Ollama endpoint", data["ai"]["error"])

    def test_system_health_ai_non_loopback_rejected(self):
        # Override model endpoint to a non-loopback address
        self.config._config["model"]["endpoint"] = "http://example.com:11434"
        self.config.save()

        res = self.client.get("/api/system/health", headers=self.headers())
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["ai"]["status"], "offline")
        self.assertIn("not a loopback URL", data["ai"]["error"])

    @patch("Mailroom.gmail._read_last_email")
    @patch("Mailroom.gmail.check_gmail_dependencies")
    def test_system_health_gmail_connected(self, mock_deps, mock_read):
        mock_deps.return_value = (True, [])
        mock_read.return_value = "user@example.com"

        res = self.client.get("/api/system/health", headers=self.headers())
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["gmail"]["status"], "connected")
        self.assertEqual(data["gmail"]["account"], "user@example.com")

    def test_system_health_host_header_enforced(self):
        res = self.client.get("/api/system/health", headers=self.headers(host="evil.com"))
        self.assertEqual(res.status_code, 400)

    # --- Onboarding Setup Endpoint Tests ---

    def test_config_setup_standard_profile_and_taxonomy(self):
        payload = {
            "profile": "standard",
            "taxonomy": "standard",
        }
        res = self.client.post(
            "/api/config/setup",
            headers=self.headers(),
            json=payload,
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["profile"], "standard")
        self.assertEqual(data["taxonomy"], "standard")

        # Verify config saved to disk
        reloaded = Config(self.config_path)
        self.assertTrue(reloaded.get("onboarding_completed"))
        self.assertEqual(reloaded.get("hardware_profile"), "standard")
        self.assertEqual(reloaded.get("taxonomy_archetype"), "standard")
        self.assertEqual(reloaded.model_id, "qwen2.5:7b")
        self.assertEqual(reloaded.scan_query, Config.DEFAULT_SCAN_QUERY)

        # Verify database was initialized
        db_file = Path(reloaded.database_path)
        self.assertTrue(db_file.exists())
        conn = sqlite3.connect(str(db_file))
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='messages'")
        self.assertIsNotNone(cursor.fetchone())
        conn.close()

    def test_config_setup_lightweight_profile_single_label(self):
        payload = {
            "profile": "lightweight",
            "taxonomy": "single-label",
            "target_label": "Receipts",
        }
        res = self.client.post(
            "/api/config/setup",
            headers=self.headers(),
            json=payload,
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["profile"], "lightweight")
        self.assertEqual(data["taxonomy"], "single-label")

        reloaded = Config(self.config_path)
        self.assertTrue(reloaded.get("onboarding_completed"))
        self.assertEqual(reloaded.get("hardware_profile"), "lightweight")
        self.assertEqual(reloaded.model_id, "qwen2.5:3b")
        self.assertEqual(len(reloaded.labels), 1)
        self.assertEqual(reloaded.labels[0]["name"], "Receipts")
        self.assertEqual(reloaded.scan_query, Config.DOCUMENTS_ATTACHMENT_QUERY)

    def test_config_setup_skip(self):
        payload = {"skip": True}
        res = self.client.post(
            "/api/config/setup",
            headers=self.headers(),
            json=payload,
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["status"], "success")
        self.assertTrue(data["skipped"])

        reloaded = Config(self.config_path)
        self.assertTrue(reloaded.get("onboarding_completed"))
        self.assertTrue(Path(reloaded.database_path).exists())

    def test_config_setup_invalid_profile(self):
        payload = {"profile": "quantum-supercomputer", "taxonomy": "standard"}
        res = self.client.post(
            "/api/config/setup",
            headers=self.headers(),
            json=payload,
        )
        self.assertEqual(res.status_code, 400)
        data = res.get_json()
        self.assertIn("Invalid profile", data["error"])

    def test_config_setup_invalid_taxonomy(self):
        payload = {"profile": "standard", "taxonomy": "invalid-taxonomy"}
        res = self.client.post(
            "/api/config/setup",
            headers=self.headers(),
            json=payload,
        )
        self.assertEqual(res.status_code, 400)
        data = res.get_json()
        self.assertIn("Invalid taxonomy", data["error"])

    def test_config_setup_invalid_target_label(self):
        payload = {"profile": "standard", "taxonomy": "single-label", "target_label": "   "}
        res = self.client.post(
            "/api/config/setup",
            headers=self.headers(),
            json=payload,
        )
        self.assertEqual(res.status_code, 400)
        data = res.get_json()
        self.assertIn("target_label", data["error"])

    def test_config_setup_csrf_rejected(self):
        payload = {"profile": "standard", "taxonomy": "standard"}
        res = self.client.post(
            "/api/config/setup",
            headers=self.headers(csrf=False),
            json=payload,
        )
        self.assertEqual(res.status_code, 403)

    def test_config_setup_host_header_rejected(self):
        payload = {"profile": "standard", "taxonomy": "standard"}
        res = self.client.post(
            "/api/config/setup",
            headers=self.headers(host="malicious.attacker.com"),
            json=payload,
        )
        self.assertEqual(res.status_code, 400)
