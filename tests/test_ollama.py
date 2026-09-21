"""Unit tests for Ollama backend provider and multi-hardware support."""

import json
import io
import os
import shutil
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import requests

from EmailMan import db as db_module

from EmailMan.classification import (
    ClassificationError,
    PROPOSAL_JSON_SCHEMA,
    PROVIDER_OLLAMA,
    PROVIDER_OPENAI,
    PROVIDER_TABBY,
    Proposal,
    TabbyClient,
    classify_message,
    detect_provider,
    probe_model_endpoint,
)
from EmailMan.config import Config, ConfigError, HARDWARE_PROFILES


class TestOllamaProviderDetection(unittest.TestCase):
    """Test endpoint auto-detection and provider selection."""

    def test_detect_ollama_by_port(self):
        self.assertEqual(detect_provider("http://127.0.0.1:11434"), PROVIDER_OLLAMA)
        self.assertEqual(detect_provider("http://localhost:11434"), PROVIDER_OLLAMA)

    def test_detect_ollama_by_path(self):
        self.assertEqual(detect_provider("http://127.0.0.1:8000/api/generate"), PROVIDER_OLLAMA)
        self.assertEqual(detect_provider("http://127.0.0.1:8000/api/tags"), PROVIDER_OLLAMA)

    def test_detect_openai_by_path(self):
        self.assertEqual(
            detect_provider("http://127.0.0.1:8090/v1/chat/completions"), PROVIDER_OPENAI
        )
        self.assertEqual(detect_provider("http://127.0.0.1:8090/v1"), PROVIDER_OPENAI)

    def test_detect_tabby_by_path_or_port(self):
        self.assertEqual(detect_provider("http://127.0.0.1:8080/completion"), PROVIDER_TABBY)
        self.assertEqual(detect_provider("http://127.0.0.1:8080"), PROVIDER_TABBY)

    def test_explicit_provider_override(self):
        self.assertEqual(
            detect_provider("http://127.0.0.1:9999", explicit_provider="ollama"),
            PROVIDER_OLLAMA,
        )
        self.assertEqual(
            detect_provider("http://127.0.0.1:11434", explicit_provider="openai"),
            PROVIDER_OPENAI,
        )

    def test_detect_ollama_ipv6_and_query(self):
        self.assertEqual(detect_provider("http://[::1]:11434"), PROVIDER_OLLAMA)
        self.assertEqual(detect_provider("http://127.0.0.1:11434/api/generate?debug=1"), PROVIDER_OLLAMA)

    def test_detect_ollama_subpath_on_custom_port(self):
        self.assertEqual(
            detect_provider("http://127.0.0.1:8080/ollama/api/generate"),
            PROVIDER_OLLAMA,
        )

    def test_ollama_base_preserves_subpath(self):
        from EmailMan.classification import ollama_base

        self.assertEqual(
            ollama_base("http://127.0.0.1:8080/ollama/api/generate"),
            "http://127.0.0.1:8080/ollama",
        )
        self.assertEqual(
            ollama_base("http://127.0.0.1:11434/api/tags"),
            "http://127.0.0.1:11434",
        )


class TestOllamaProbing(unittest.TestCase):
    """Test Ollama model probing via /api/tags."""

    def setUp(self):
        self.endpoint = "http://127.0.0.1:11434"
        self.client = TabbyClient(
            endpoint=self.endpoint,
            timeout=5.0,
            max_retries=2,
            model_id="qwen2.5:7b",
        )

    @patch("EmailMan.classification.requests.Session")
    def test_probe_endpoint_success(self, mock_session_cls):
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "models": [
                {"name": "qwen2.5:7b", "model": "qwen2.5:7b"},
                {"name": "llama3.2:3b", "model": "llama3.2:3b"},
            ]
        }
        mock_session = mock_session_cls.return_value
        mock_session.get.return_value = mock_response

        result = self.client.probe_endpoint()

        self.assertEqual(result["endpoint"], self.endpoint)
        self.assertEqual(result["model_id"], "qwen2.5:7b")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["provider"], PROVIDER_OLLAMA)
        self.assertIn("qwen2.5:7b", result["available_models"])
        mock_session.get.assert_called_once_with(
            "http://127.0.0.1:11434/api/tags",
            headers={"Content-Type": "application/json"},
            timeout=5.0,
        )

    @patch("EmailMan.classification.requests.Session")
    def test_probe_endpoint_resolves_unknown_model(self, mock_session_cls):
        client = TabbyClient(endpoint="http://127.0.0.1:11434", timeout=5.0)
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "models": [{"name": "mistral:7b"}]
        }
        mock_session = mock_session_cls.return_value
        mock_session.get.return_value = mock_response

        result = client.probe_endpoint()
        self.assertEqual(result["model_id"], "mistral:7b")
        self.assertEqual(client.model_id, "mistral:7b")

    @patch("EmailMan.classification.requests.Session")
    def test_probe_endpoint_retry_on_transient_error(self, mock_session_cls):
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"models": [{"name": "qwen2.5:7b"}]}
        mock_session = mock_session_cls.return_value
        mock_session.get.side_effect = [
            Mock(status_code=503, text="Unavailable"),
            mock_response,
        ]

        result = self.client.probe_endpoint()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(mock_session.get.call_count, 2)

    @patch("EmailMan.classification.requests.Session")
    def test_probe_endpoint_timeout(self, mock_session_cls):
        mock_session = mock_session_cls.return_value
        mock_session.get.side_effect = requests.exceptions.Timeout("Timeout")

        with self.assertRaises(ClassificationError) as ctx:
            self.client.probe_endpoint()
        self.assertIn("timeout", str(ctx.exception).lower())

    @patch("EmailMan.classification.requests.Session")
    def test_probe_endpoint_non_dict_json(self, mock_session_cls):
        mock_response = Mock(status_code=200)
        mock_response.json.return_value = ["not", "a", "dict"]
        mock_session = mock_session_cls.return_value
        mock_session.get.return_value = mock_response

        with self.assertRaises(ClassificationError) as ctx:
            self.client.probe_endpoint()
        self.assertIn("Malformed response", str(ctx.exception))

    @patch("EmailMan.classification.requests.Session")
    def test_probe_endpoint_empty_or_missing_models(self, mock_session_cls):
        client = TabbyClient("http://127.0.0.1:11434")
        mock_response = Mock(status_code=200)
        mock_response.json.return_value = {"models": []}
        mock_session = mock_session_cls.return_value
        mock_session.get.return_value = mock_response

        result = client.probe_endpoint()
        self.assertEqual(result["model_id"], "unknown")


class TestOllamaClassification(unittest.TestCase):
    """Test message classification and structured outputs using Ollama."""

    def setUp(self):
        self.endpoint = "http://127.0.0.1:11434"
        self.client = TabbyClient(
            endpoint=self.endpoint,
            timeout=5.0,
            max_retries=2,
            model_id="qwen2.5:7b",
        )
        self.allowed_labels = [
            "Type/Receipt",
            "Type/Newsletter",
            "Purchase/Tech",
            "Retention/Forever",
            "Retention/30Days",
        ]

    @patch("EmailMan.classification.requests.Session")
    def test_classify_message_structured_schema(self, mock_session_cls):
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "model": "qwen2.5:7b",
            "response": json.dumps(
                {
                    "label_ids": ["Type/Receipt", "Purchase/Tech", "Retention/Forever"],
                    "reason": "Cloud server invoice",
                    "abstain": False,
                }
            ),
            "done": True,
        }
        mock_session = mock_session_cls.return_value
        mock_session.post.return_value = mock_response

        proposal = self.client.classify_message(
            email_text="From: AWS\nSubject: Invoice\nTotal: $10",
            label_ids=self.allowed_labels,
        )

        self.assertIsInstance(proposal, Proposal)
        self.assertEqual(
            proposal.label_ids,
            ["Type/Receipt", "Purchase/Tech", "Retention/Forever"],
        )
        self.assertEqual(proposal.reason, "Cloud server invoice")
        self.assertFalse(proposal.abstain)

        mock_session.post.assert_called_once()
        call_args = mock_session.post.call_args
        self.assertEqual(call_args[0][0], "http://127.0.0.1:11434/api/generate")
        payload = call_args[1]["json"]
        self.assertEqual(payload["model"], "qwen2.5:7b")
        self.assertEqual(payload["format"], PROPOSAL_JSON_SCHEMA)
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["options"], {"temperature": 0, "num_predict": 300})

    @patch("EmailMan.classification.requests.Session")
    def test_classify_message_abstain(self, mock_session_cls):
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "model": "qwen2.5:7b",
            "response": json.dumps(
                {
                    "label_ids": [],
                    "reason": "Ambiguous notification",
                    "abstain": True,
                }
            ),
            "done": True,
        }
        mock_session = mock_session_cls.return_value
        mock_session.post.return_value = mock_response

        proposal = self.client.classify_message(
            email_text="Empty ping",
            label_ids=self.allowed_labels,
        )
        self.assertTrue(proposal.abstain)
        self.assertEqual(proposal.label_ids, [])

    @patch("EmailMan.classification.requests.Session")
    def test_classify_message_fallback_on_schema_rejection(self, mock_session_cls):
        mock_bad_request = Mock(status_code=400, text="format schema not supported")
        mock_success = Mock(
            status_code=200,
            json=lambda: {
                "model": "qwen2.5:7b",
                "response": json.dumps(
                    {
                        "label_ids": ["Type/Newsletter", "Retention/30Days"],
                        "reason": "Weekly update",
                        "abstain": False,
                    }
                ),
            },
        )
        mock_session = mock_session_cls.return_value
        mock_session.post.side_effect = [mock_bad_request, mock_success]

        proposal = self.client.classify_message(
            email_text="Weekly tech digest",
            label_ids=self.allowed_labels,
        )
        self.assertEqual(proposal.label_ids, ["Type/Newsletter", "Retention/30Days"])
        second_call = mock_session.post.call_args_list[1]
        self.assertEqual(second_call[1]["json"]["format"], "json")

    @patch("EmailMan.classification.requests.Session")
    def test_classify_message_fallback_failure_preserves_error(self, mock_session_cls):
        mock_bad_request = Mock(status_code=400, text="format schema error")
        mock_fallback_fail = Mock(status_code=422, text="Unprocessable Entity")
        mock_session = mock_session_cls.return_value
        mock_session.post.side_effect = [mock_bad_request, mock_fallback_fail]

        with self.assertRaises(ClassificationError) as ctx:
            self.client.classify_message("email text", label_ids=self.allowed_labels)
        self.assertIn("422", str(ctx.exception))
        self.assertIn("Unprocessable Entity", str(ctx.exception))

    @patch("EmailMan.classification.requests.Session")
    def test_complete_json_ollama(self, mock_session_cls):
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "model": "qwen2.5:7b",
            "response": json.dumps({"summary": "Clean mail", "count": 1}),
        }
        mock_session = mock_session_cls.return_value
        mock_session.post.return_value = mock_response

        res = self.client.complete_json("Summarize input")
        self.assertEqual(res, {"summary": "Clean mail", "count": 1})
        call_payload = mock_session.post.call_args[1]["json"]
        self.assertEqual(call_payload["format"], "json")

    @patch("EmailMan.classification.TabbyClient.classify_message")
    def test_convenience_classify_message_with_provider(self, mock_classify):
        mock_classify.return_value = Proposal(label_ids=[], reason="test", abstain=True)
        res = classify_message(
            email_text="test",
            label_ids=["Type/Receipt"],
            model_endpoint="http://127.0.0.1:11434",
            provider="ollama",
        )
        self.assertTrue(res.abstain)
        mock_classify.assert_called_once()

    @patch("EmailMan.classification.TabbyClient.probe_endpoint")
    def test_convenience_probe_model_endpoint_with_provider(self, mock_probe):
        mock_probe.return_value = {"status": "ready"}
        res = probe_model_endpoint("http://127.0.0.1:11434", provider="ollama")
        self.assertEqual(res["status"], "ready")
        mock_probe.assert_called_once()


class TestHardwareProfilesAndConfig(unittest.TestCase):
    """Test multi-hardware profiles and configuration validation."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.temp_dir, "config.json")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_hardware_profiles_catalog(self):
        self.assertIn("standard", HARDWARE_PROFILES)
        self.assertIn("lightweight", HARDWARE_PROFILES)
        self.assertIn("tabby", HARDWARE_PROFILES)

        standard = HARDWARE_PROFILES["standard"]
        self.assertEqual(standard["provider"], "ollama")
        self.assertEqual(standard["endpoint"], "http://127.0.0.1:11434")
        self.assertEqual(standard["id"], "qwen2.5:7b")

        lightweight = HARDWARE_PROFILES["lightweight"]
        self.assertEqual(lightweight["provider"], "ollama")
        self.assertEqual(lightweight["endpoint"], "http://127.0.0.1:11434")
        self.assertEqual(lightweight["id"], "qwen2.5:3b")

    def test_config_init_with_standard_profile(self):
        cfg = Config(self.config_path, profile="standard")
        self.assertEqual(cfg.model_endpoint, "http://127.0.0.1:11434")
        self.assertEqual(cfg.model_id, "qwen2.5:7b")
        self.assertEqual(cfg.model_provider, "ollama")
        cfg.validate()

    def test_config_init_with_lightweight_profile(self):
        cfg = Config(self.config_path, profile="lightweight")
        self.assertEqual(cfg.model_endpoint, "http://127.0.0.1:11434")
        self.assertEqual(cfg.model_id, "qwen2.5:3b")
        self.assertEqual(cfg.model_provider, "ollama")
        cfg.validate()

    def test_config_init_default_profile_is_ollama_standard(self):
        cfg = Config(self.config_path)
        self.assertEqual(cfg.model_endpoint, "http://127.0.0.1:11434")
        self.assertEqual(cfg.model_id, "qwen2.5:7b")
        self.assertEqual(cfg.model_provider, "ollama")
        cfg.validate()

    def test_create_config_profile_defaults_to_standard(self):
        from EmailMan import cli as cli_module

        args = cli_module.create_cli_parser().parse_args(["create-config"])
        self.assertEqual(args.profile, "standard")

    def test_create_config_stdout_names_tabby_backend(self):
        from EmailMan import cli as cli_module

        target = os.path.join(self.temp_dir, "tabby", "config.json")
        args = cli_module.create_cli_parser().parse_args(
            ["--config", target, "create-config", "--profile", "tabby"]
        )
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            code = cli_module.create_config_cmd(args)

        self.assertEqual(code, 0)
        out = stdout.getvalue()
        tabby = HARDWARE_PROFILES["tabby"]
        self.assertIn(f"{tabby['provider']} {tabby['id']} on {tabby['endpoint']}", out)
        self.assertNotIn("qwen2.5:7b", out)
        self.assertNotIn("use --profile tabby", out)

    def test_create_config_stdout_names_standard_backend(self):
        from EmailMan import cli as cli_module

        target = os.path.join(self.temp_dir, "standard", "config.json")
        args = cli_module.create_cli_parser().parse_args(["--config", target, "create-config"])
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            code = cli_module.create_config_cmd(args)

        self.assertEqual(code, 0)
        out = stdout.getvalue()
        standard = HARDWARE_PROFILES["standard"]
        self.assertIn(f"{standard['provider']} {standard['id']} on {standard['endpoint']}", out)
        self.assertIn("use --profile tabby", out)

    def test_config_validation_rejects_invalid_provider(self):
        cfg = Config(self.config_path)
        cfg._config["model"]["provider"] = "unsupported_cloud"
        with self.assertRaises(ConfigError) as ctx:
            cfg.validate()
        self.assertIn("Invalid model provider", str(ctx.exception))


class TestOllamaSecurityAndIsolation(unittest.TestCase):
    """Test security constraints and proxy isolation for Ollama."""

    def test_reject_non_loopback_ollama_endpoint(self):
        with self.assertRaises(ClassificationError) as ctx:
            TabbyClient("http://remote-gpu:11434")
        self.assertIn("loopback", str(ctx.exception).lower())

    def test_session_isolation_trust_env_false(self):
        client = TabbyClient("http://127.0.0.1:11434")
        session = client._get_session()
        self.assertFalse(session.trust_env)
        self.assertEqual(session.proxies.get("http"), None)
        self.assertEqual(session.proxies.get("https"), None)


class TestOllamaCliAndApiOverrides(unittest.TestCase):
    """Verify endpoint override behavior in CLI and API routes."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.tmp_dir.name) / "config.json"
        self.config = Config(self.config_path)
        self.config._config["model"]["provider"] = "tabby"
        self.config._config["model"]["endpoint"] = "http://127.0.0.1:8080/completion"
        self.config.save()
        db_obj = db_module.DB(self.config.database_path)
        self.account_id = db_obj.get_or_create_account("user@example.com")
        db_obj.upsert_message(
            self.account_id,
            {
                "gmail_message_id": "gm-1",
                "thread_id": "th-1",
                "sender": "Service",
                "sender_email": "service@example.com",
                "subject": "Monthly Statement",
                "body_preview": "Your statement is ready",
                "received_at": None,
                "gmail_labels": [],
                "truncated": False,
                "unsupported_content": False,
                "has_attachments": False,
            },
        )
        db_obj.close()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_cli_classify_endpoint_override_clears_explicit_provider(self):
        from EmailMan import cli as cli_module

        args = cli_module.create_cli_parser().parse_args(
            [
                "--config",
                str(self.config_path),
                "classify",
                "--message-id",
                "user@example.com:gm-1",
                "--model-endpoint",
                "http://127.0.0.1:11434",
            ]
        )
        with patch(
            "EmailMan.classification.classify_message",
            return_value=Proposal(label_ids=["Type/Receipt"], reason="monthly", abstain=False),
        ) as mock_classify:
            code = cli_module.classify_cmd(args, self.config)
        self.assertEqual(code, 0)
        self.assertIsNone(mock_classify.call_args.kwargs["provider"])
        self.assertEqual(mock_classify.call_args.kwargs["model_endpoint"], "http://127.0.0.1:11434")

    def test_api_classify_endpoint_override_clears_explicit_provider(self):
        from EmailMan import app as app_module

        app = app_module.create_app(self.config)
        client = app.test_client()
        with patch(
            "EmailMan.classification.classify_message",
            return_value=Proposal(label_ids=["Type/Receipt"], reason="monthly", abstain=False),
        ) as mock_classify:
            res = client.post(
                "/api/classify",
                json={
                    "message_id": "user@example.com:gm-1",
                    "model_endpoint": "http://127.0.0.1:11434",
                },
                headers={"X-CSRF-Token": app.config["CSRF_TOKEN"]},
            )
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(mock_classify.call_args.kwargs["provider"])
        self.assertEqual(mock_classify.call_args.kwargs["model_endpoint"], "http://127.0.0.1:11434")

    def test_api_classify_endpoint_override_with_explicit_provider(self):
        from EmailMan import app as app_module

        app = app_module.create_app(self.config)
        client = app.test_client()
        with patch(
            "EmailMan.classification.classify_message",
            return_value=Proposal(label_ids=["Type/Receipt"], reason="monthly", abstain=False),
        ) as mock_classify:
            res = client.post(
                "/api/classify",
                json={
                    "message_id": "user@example.com:gm-1",
                    "model_endpoint": "http://127.0.0.1:11434",
                    "provider": "ollama",
                },
                headers={"X-CSRF-Token": app.config["CSRF_TOKEN"]},
            )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(mock_classify.call_args.kwargs["provider"], "ollama")


if __name__ == "__main__":
    unittest.main()
