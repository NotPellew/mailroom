"""End-to-end tests for visual local AI status and model download APIs."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from Mailroom import app as app_module
from Mailroom.config import Config


class TestWebOllama(unittest.TestCase):
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

    @patch("requests.Session.get")
    def test_status_ollama_offline(self, mock_get):
        import requests
        mock_get.side_effect = requests.exceptions.ConnectionError("Connection refused")

        res = self.client.get("/api/ollama/status", headers=self.headers())
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertFalse(data["running"])
        self.assertFalse(data["model_installed"])
        self.assertFalse(data["ready"])
        self.assertIn("Connection refused", data["error"])

    @patch("requests.Session.get")
    def test_status_ollama_online_missing_model(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "models": [{"name": "llama3:8b", "model": "llama3:8b"}]
        }
        mock_get.return_value = mock_resp

        res = self.client.get("/api/ollama/status", headers=self.headers())
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["running"])
        self.assertFalse(data["model_installed"])
        self.assertFalse(data["ready"])
        self.assertIn("llama3:8b", data["available_models"])

    @patch("requests.Session.get")
    def test_status_ollama_online_model_installed(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "models": [
                {"name": "qwen2.5:7b", "model": "qwen2.5:7b"},
                {"name": "llama3:8b", "model": "llama3:8b"},
            ]
        }
        mock_get.return_value = mock_resp

        res = self.client.get("/api/ollama/status", headers=self.headers())
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["running"])
        self.assertTrue(data["model_installed"])
        self.assertTrue(data["ready"])
        self.assertEqual(data["model"], "qwen2.5:7b")

    @patch("requests.Session.get")
    def test_status_ollama_tag_matching(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "models": [{"name": "qwen2.5:7b:latest", "model": "qwen2.5:7b:latest"}]
        }
        mock_get.return_value = mock_resp

        res = self.client.get("/api/ollama/status", headers=self.headers())
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["model_installed"])
        self.assertTrue(data["ready"])

    @patch("Mailroom.classification.probe_model_endpoint")
    def test_status_non_ollama_provider(self, mock_probe):
        mock_probe.return_value = {"status": "ready"}

        # Configure Tabby provider
        self.config._config["model"]["provider"] = "tabby"
        self.config._config["model"]["endpoint"] = "http://127.0.0.1:8080/completion"
        self.config._config["model"]["id"] = "Qwen2.5-7B-Instruct-EXL3"
        self.config.save()
        app = app_module.create_app(self.config)
        client = app.test_client()

        res = client.get("/api/ollama/status", headers=self.headers())
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["provider"], "tabby")
        self.assertTrue(data["running"])
        self.assertTrue(data["ready"])

    def test_status_host_header_enforcement(self):
        res = self.client.get("/api/ollama/status", headers={"Host": "evil.example"})
        self.assertEqual(res.status_code, 400)

    @patch("requests.Session.post")
    def test_pull_streams_sse_progress(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.iter_lines.return_value = [
            b'{"status": "pulling manifest"}',
            b'{"status": "downloading layer", "completed": 2147483648, "total": 4294967296}',
            b'{"status": "success"}',
        ]
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = None
        mock_post.return_value = mock_resp

        res = self.client.post(
            "/api/ollama/pull",
            json={"model": "qwen2.5:7b"},
            headers=self.headers(),
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/event-stream", res.mimetype)

        text = res.get_data(as_text=True)
        lines = [line.strip() for line in text.split("\n") if line.startswith("data:")]
        self.assertEqual(len(lines), 3)

        first_event = json.loads(lines[0][len("data:"):].strip())
        self.assertEqual(first_event["status"], "pulling manifest")

        second_event = json.loads(lines[1][len("data:"):].strip())
        self.assertEqual(second_event["completed"], 2147483648)
        self.assertEqual(second_event["total"], 4294967296)

        third_event = json.loads(lines[2][len("data:"):].strip())
        self.assertEqual(third_event["status"], "success")

        # Verify correct payload passed to Ollama API: {"name": ..., "stream": True}
        call_kwargs = mock_post.call_args[1]
        self.assertEqual(call_kwargs["json"], {"name": "qwen2.5:7b", "stream": True})
        self.assertEqual(call_kwargs["timeout"], (5.0, None))

    def test_pull_non_ollama_rejected(self):
        self.config._config["model"]["provider"] = "tabby"
        self.config._config["model"]["endpoint"] = "http://127.0.0.1:8080/completion"
        self.config._config["model"]["id"] = "Qwen2.5-7B-Instruct-EXL3"
        self.config.save()
        app = app_module.create_app(self.config)
        client = app.test_client()

        csrf = app.config["CSRF_TOKEN"]
        res = client.post(
            "/api/ollama/pull",
            json={"model": "qwen2.5:7b"},
            headers={"Origin": "http://127.0.0.1:5000", "X-CSRF-Token": csrf},
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("only supported when using Ollama", res.get_json()["error"])

    def test_pull_csrf_rejected_without_token(self):
        res = self.client.post(
            "/api/ollama/pull",
            json={"model": "qwen2.5:7b"},
            headers=self.headers(csrf=False),
        )
        self.assertEqual(res.status_code, 403)

    def test_pull_malicious_model_rejected(self):
        # Path traversal
        res1 = self.client.post(
            "/api/ollama/pull",
            json={"model": "../../etc/passwd"},
            headers=self.headers(),
        )
        self.assertEqual(res1.status_code, 400)

        # Illegal characters
        res2 = self.client.post(
            "/api/ollama/pull",
            json={"model": "model;rm -rf /"},
            headers=self.headers(),
        )
        self.assertEqual(res2.status_code, 400)

        # Name too long (> 128)
        res3 = self.client.post(
            "/api/ollama/pull",
            json={"model": "a" * 130},
            headers=self.headers(),
        )
        self.assertEqual(res3.status_code, 400)

    @patch("requests.Session.post")
    def test_pull_upstream_error_yields_error_event(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = None
        mock_post.return_value = mock_resp

        res = self.client.post(
            "/api/ollama/pull",
            json={"model": "qwen2.5:7b"},
            headers=self.headers(),
        )
        self.assertEqual(res.status_code, 200)
        text = res.get_data(as_text=True)
        self.assertIn("Ollama error HTTP 500", text)
