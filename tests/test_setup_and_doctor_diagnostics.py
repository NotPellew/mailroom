"""Tests for granular doctor diagnostics and the interactive setup onboarding wizard."""

import argparse
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from Mailroom import cli as cli_module
from Mailroom import config as config_module
from Mailroom import db as db_module


class TestDoctorDiagnostics(unittest.TestCase):
    """Test doctor model presence, OAuth scope checks, and client credentials inspection."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.tmpdir.name) / "config.json"
        self.cfg = config_module.Config(str(self.config_path))
        db_obj = db_module.DB(self.cfg.database_path)
        db_obj.close()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_doctor_ollama_missing_model_notice(self):
        """doctor reports missing Ollama models with the exact pull command notice."""
        args = argparse.Namespace(strict=False)
        self.cfg._config["model"]["provider"] = "ollama"
        self.cfg._config["model"]["id"] = "qwen2.5:7b"

        mock_probe = {
            "endpoint": self.cfg.model_endpoint,
            "model_id": "other:3b",
            "status": "ready",
            "provider": "ollama",
            "available_models": ["other:3b"],
        }
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            with patch("Mailroom.classification.probe_model_endpoint", return_value=mock_probe):
                code = cli_module.doctor_cmd(args, self.cfg)

        output = stdout.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("[!] Model 'qwen2.5:7b' is not installed locally. Run: ollama pull qwen2.5:7b", output)
        self.assertIn("ollama pull qwen2.5:7b", output)

    def test_doctor_client_credentials_valid_installed(self):
        """doctor recognizes a valid Desktop app credentials.json file with 'installed' block."""
        cred_path = self.cfg.config_dir / "credentials.json"
        cred_path.write_text(json.dumps({"installed": {"client_id": "desktop-id.apps.googleusercontent.com"}}))

        args = argparse.Namespace(strict=False)
        mock_probe = {
            "endpoint": self.cfg.model_endpoint,
            "model_id": self.cfg.model_id,
            "status": "ready",
            "provider": "tabby",
        }
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            with patch("Mailroom.classification.probe_model_endpoint", return_value=mock_probe):
                code = cli_module.doctor_cmd(args, self.cfg)

        output = stdout.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("[✓] Client credentials: credentials.json valid (desktop 'installed' client)", output)

    def test_doctor_client_credentials_web_type_warning(self):
        """doctor warns when credentials.json has a 'web' block instead of 'installed'."""
        cred_path = self.cfg.config_dir / "credentials.json"
        cred_path.write_text(json.dumps({"web": {"client_id": "web-id.apps.googleusercontent.com"}}))

        args = argparse.Namespace(strict=False)
        mock_probe = {
            "endpoint": self.cfg.model_endpoint,
            "model_id": self.cfg.model_id,
            "status": "ready",
            "provider": "tabby",
        }
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            with patch("Mailroom.classification.probe_model_endpoint", return_value=mock_probe):
                code = cli_module.doctor_cmd(args, self.cfg)

        output = stdout.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("[!] Client credentials: 'credentials.json' is missing the 'installed' desktop client block", output)

    def test_doctor_client_credentials_invalid_json(self):
        """doctor warns when credentials.json contains malformed JSON."""
        cred_path = self.cfg.config_dir / "credentials.json"
        cred_path.write_text("{malformed json:")

        args = argparse.Namespace(strict=False)
        mock_probe = {
            "endpoint": self.cfg.model_endpoint,
            "model_id": self.cfg.model_id,
            "status": "ready",
            "provider": "tabby",
        }
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            with patch("Mailroom.classification.probe_model_endpoint", return_value=mock_probe):
                code = cli_module.doctor_cmd(args, self.cfg)

        output = stdout.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("[!] Client credentials: 'credentials.json' exists but contains invalid JSON", output)

    def test_doctor_oauth_scope_validation_missing_modify(self):
        """doctor warns when stored keyring credentials lack gmail.modify scope."""
        args = argparse.Namespace(strict=False)
        mock_probe = {
            "endpoint": self.cfg.model_endpoint,
            "model_id": self.cfg.model_id,
            "status": "ready",
            "provider": "tabby",
        }
        readonly_creds = {
            "token": "tok123",
            "refresh_token": "ref123",
            "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
        }
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            with patch("Mailroom.classification.probe_model_endpoint", return_value=mock_probe):
                with patch("Mailroom.gmail.check_gmail_dependencies", return_value=(True, [])):
                    with patch("Mailroom.gmail._read_last_email", return_value="user@example.com"):
                        with patch("Mailroom.gmail._load_credentials", return_value=readonly_creds):
                            code = cli_module.doctor_cmd(args, self.cfg)

        output = stdout.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("[!] Stored Gmail credentials lack gmail.modify scope. Run: python -m Mailroom auth --reauth", output)

    def test_doctor_oauth_scope_validation_modify_present(self):
        """doctor reports valid when stored keyring credentials contain gmail.modify scope."""
        args = argparse.Namespace(strict=False)
        mock_probe = {
            "endpoint": self.cfg.model_endpoint,
            "model_id": self.cfg.model_id,
            "status": "ready",
            "provider": "tabby",
        }
        modify_creds = {
            "token": "tok123",
            "refresh_token": "ref123",
            "scopes": ["https://www.googleapis.com/auth/gmail.modify"],
        }
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            with patch("Mailroom.classification.probe_model_endpoint", return_value=mock_probe):
                with patch("Mailroom.gmail.check_gmail_dependencies", return_value=(True, [])):
                    with patch("Mailroom.gmail._read_last_email", return_value="user@example.com"):
                        with patch("Mailroom.gmail._load_credentials", return_value=modify_creds):
                            code = cli_module.doctor_cmd(args, self.cfg)

        output = stdout.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("[✓] Stored Gmail credentials: authorized with gmail.modify scope (user@example.com)", output)

    def test_doctor_oauth_scope_validation_null_scopes(self):
        """doctor handles None/null scopes defensively without TypeError."""
        args = argparse.Namespace(strict=False)
        mock_probe = {
            "endpoint": self.cfg.model_endpoint,
            "model_id": self.cfg.model_id,
            "status": "ready",
            "provider": "tabby",
        }
        null_scope_creds = {
            "token": "tok123",
            "refresh_token": "ref123",
            "scopes": None,
        }
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            with patch("Mailroom.classification.probe_model_endpoint", return_value=mock_probe):
                with patch("Mailroom.gmail.check_gmail_dependencies", return_value=(True, [])):
                    with patch("Mailroom.gmail._read_last_email", return_value="user@example.com"):
                        with patch("Mailroom.gmail._load_credentials", return_value=null_scope_creds):
                            code = cli_module.doctor_cmd(args, self.cfg)

        output = stdout.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("[!] Stored Gmail credentials lack gmail.modify scope. Run: python -m Mailroom auth --reauth", output)


class TestInteractiveSetupWizard(unittest.TestCase):
    """Test the mailroom setup onboarding wizard."""

    def test_setup_non_interactive_flag_exits_0(self):
        """setup --non-interactive exits cleanly with instructions and exit code 0."""
        parser = cli_module.create_cli_parser()
        args = parser.parse_args(["setup", "--non-interactive"])

        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            code = cli_module.setup_cmd(args)

        output = stdout.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("Mailroom setup wizard requires an interactive terminal (TTY)", output)
        self.assertIn("python -m Mailroom create-config", output)
        self.assertIn("python -m Mailroom init-db", output)
        self.assertIn("python -m Mailroom doctor", output)

    def test_setup_non_tty_stdin_exits_0(self):
        """setup exits cleanly with code 0 when stdin is not a TTY."""
        parser = cli_module.create_cli_parser()
        args = parser.parse_args(["setup"])

        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            with patch("sys.stdin.isatty", return_value=False):
                code = cli_module.setup_cmd(args)

        output = stdout.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("Mailroom setup wizard requires an interactive terminal (TTY)", output)

    def test_setup_interactive_fresh_standard(self):
        """setup creates standard configuration, initializes database, and tests backend."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "config.json"
            parser = cli_module.create_cli_parser()
            args = parser.parse_args(["--config", str(cfg_path), "setup"])

            # Inputs: choice 1 (standard profile), choice 1 (standard taxonomy)
            user_inputs = ["1", "1"]
            mock_probe = {
                "endpoint": "http://127.0.0.1:11434",
                "model_id": "qwen2.5:7b",
                "status": "ready",
                "provider": "ollama",
                "available_models": ["qwen2.5:7b"],
            }

            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                with patch("sys.stdin.isatty", return_value=True):
                    with patch("builtins.input", side_effect=user_inputs):
                        with patch("Mailroom.classification.probe_model_endpoint", return_value=mock_probe):
                            code = cli_module.setup_cmd(args)

            output = stdout.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("Welcome to Mailroom Setup Wizard", output)
            self.assertIn("[✓] Configuration created", output)
            self.assertIn("[✓] Database initialized", output)
            self.assertIn("[✓] Backend reachable and model 'qwen2.5:7b' is installed locally", output)
            self.assertIn("Mailroom Setup Complete!", output)

            # Verify files on disk
            self.assertTrue(cfg_path.exists())
            loaded_cfg = config_module.Config(str(cfg_path))
            self.assertEqual(loaded_cfg.model_id, "qwen2.5:7b")
            self.assertTrue(Path(loaded_cfg.database_path).exists())

    def test_setup_interactive_fresh_single_label_custom(self):
        """setup creates single-label taxonomy with custom target label and lightweight profile."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "config.json"
            parser = cli_module.create_cli_parser()
            args = parser.parse_args(["--config", str(cfg_path), "setup"])

            # Inputs: choice 2 (lightweight profile), choice 2 (single-label), target label: Belege
            user_inputs = ["2", "2", "Belege"]
            mock_probe = {
                "endpoint": "http://127.0.0.1:11434",
                "model_id": "qwen2.5:3b",
                "status": "ready",
                "provider": "ollama",
                "available_models": ["qwen2.5:3b"],
            }

            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                with patch("sys.stdin.isatty", return_value=True):
                    with patch("builtins.input", side_effect=user_inputs):
                        with patch("Mailroom.classification.probe_model_endpoint", return_value=mock_probe):
                            code = cli_module.setup_cmd(args)

            output = stdout.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("profile: lightweight, taxonomy: single-label", output)

            # Verify saved configuration
            loaded_cfg = config_module.Config(str(cfg_path))
            self.assertEqual(loaded_cfg.model_id, "qwen2.5:3b")
            self.assertEqual(loaded_cfg.scan_query, config_module.Config.DOCUMENTS_ATTACHMENT_QUERY)
            label_ids = [label["id"] for label in loaded_cfg.labels]
            self.assertEqual(label_ids, ["Belege"])

    def test_setup_interactive_preserves_existing_config_when_declined(self):
        """setup does not overwrite existing config without explicit user confirmation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "config.json"
            # Create pre-existing config with custom setting
            pre_cfg = config_module.Config(str(cfg_path), profile="tabby")
            initial_content = cfg_path.read_text()

            parser = cli_module.create_cli_parser()
            args = parser.parse_args(["--config", str(cfg_path), "setup"])

            # User declines overwrite with 'n'
            user_inputs = ["n"]
            mock_probe = {
                "endpoint": pre_cfg.model_endpoint,
                "model_id": pre_cfg.model_id,
                "status": "ready",
                "provider": "tabby",
            }

            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                with patch("sys.stdin.isatty", return_value=True):
                    with patch("builtins.input", side_effect=user_inputs):
                        with patch("Mailroom.classification.probe_model_endpoint", return_value=mock_probe):
                            code = cli_module.setup_cmd(args)

            output = stdout.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("Keeping existing configuration", output)
            self.assertEqual(cfg_path.read_text(), initial_content)

    def test_setup_interactive_overwrites_existing_config_when_confirmed(self):
        """setup updates config when user explicitly confirms overwrite."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "config.json"
            config_module.Config(str(cfg_path), profile="tabby")

            parser = cli_module.create_cli_parser()
            args = parser.parse_args(["--config", str(cfg_path), "setup"])

            # User confirms overwrite ('y'), chooses lightweight (2), standard taxonomy (1)
            user_inputs = ["y", "2", "1"]
            mock_probe = {
                "endpoint": "http://127.0.0.1:11434",
                "model_id": "qwen2.5:3b",
                "status": "ready",
                "provider": "ollama",
                "available_models": ["qwen2.5:3b"],
            }

            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                with patch("sys.stdin.isatty", return_value=True):
                    with patch("builtins.input", side_effect=user_inputs):
                        with patch("Mailroom.classification.probe_model_endpoint", return_value=mock_probe):
                            code = cli_module.setup_cmd(args)

            output = stdout.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("[✓] Configuration created", output)
            updated_cfg = config_module.Config(str(cfg_path))
            self.assertEqual(updated_cfg.model_id, "qwen2.5:3b")

    def test_setup_backend_unreachable_instruction(self):
        """setup outputs ollama serve and pull instructions when backend is offline."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "config.json"
            parser = cli_module.create_cli_parser()
            args = parser.parse_args(["--config", str(cfg_path), "setup"])

            user_inputs = ["1", "1"]
            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                with patch("sys.stdin.isatty", return_value=True):
                    with patch("builtins.input", side_effect=user_inputs):
                        with patch("Mailroom.classification.probe_model_endpoint", side_effect=Exception("Connection refused")):
                            code = cli_module.setup_cmd(args)

            output = stdout.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("unable to connect to", output)
            self.assertIn("ollama serve", output)
            self.assertIn("ollama pull qwen2.5:7b", output)
            self.assertIn("Mailroom Setup Complete!", output)

    def test_setup_cancellation_on_keyboard_interrupt(self):
        """setup exits cleanly with 130 when user hits Ctrl+C."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "config.json"
            parser = cli_module.create_cli_parser()
            args = parser.parse_args(["--config", str(cfg_path), "setup"])

            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                with patch("sys.stdin.isatty", return_value=True):
                    with patch("builtins.input", side_effect=KeyboardInterrupt):
                        code = cli_module.setup_cmd(args)

            output = stdout.getvalue()
            self.assertEqual(code, 130)
            self.assertIn("Setup cancelled", output)

    def test_setup_interactive_gmail_auth_prompt_declined(self):
        """setup skips auth when credentials.json exists but user declines prompt."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "config.json"
            cred_path = Path(tmpdir) / "credentials.json"
            cred_path.write_text(json.dumps({"installed": {"client_id": "test"}}))

            parser = cli_module.create_cli_parser()
            args = parser.parse_args(["--config", str(cfg_path), "setup"])

            # Inputs: profile 1, taxonomy 1, auth prompt 'n'
            user_inputs = ["1", "1", "n"]
            mock_probe = {
                "endpoint": "http://127.0.0.1:11434",
                "model_id": "qwen2.5:7b",
                "status": "ready",
                "provider": "ollama",
                "available_models": ["qwen2.5:7b"],
            }

            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                with patch("sys.stdin.isatty", return_value=True):
                    with patch("builtins.input", side_effect=user_inputs):
                        with patch("Mailroom.classification.probe_model_endpoint", return_value=mock_probe):
                            with patch("Mailroom.gmail.check_gmail_dependencies", return_value=(True, [])):
                                with patch("Mailroom.gmail._read_last_email", return_value=None):
                                    code = cli_module.setup_cmd(args)

            output = stdout.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("Found OAuth client credentials at", output)
            self.assertIn("Skipping Gmail authentication for now", output)

    def test_setup_interactive_gmail_auth_prompt_accepted(self):
        """setup executes authenticate when credentials.json exists and user confirms prompt."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "config.json"
            cred_path = Path(tmpdir) / "credentials.json"
            cred_path.write_text(json.dumps({"installed": {"client_id": "test"}}))

            parser = cli_module.create_cli_parser()
            args = parser.parse_args(["--config", str(cfg_path), "setup"])

            # Inputs: profile 1, taxonomy 1, auth prompt 'y'
            user_inputs = ["1", "1", "y"]
            mock_probe = {
                "endpoint": "http://127.0.0.1:11434",
                "model_id": "qwen2.5:7b",
                "status": "ready",
                "provider": "ollama",
                "available_models": ["qwen2.5:7b"],
            }

            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                with patch("sys.stdin.isatty", return_value=True):
                    with patch("builtins.input", side_effect=user_inputs):
                        with patch("Mailroom.classification.probe_model_endpoint", return_value=mock_probe):
                            with patch("Mailroom.gmail.check_gmail_dependencies", return_value=(True, [])):
                                with patch("Mailroom.gmail._read_last_email", return_value=None):
                                    with patch("Mailroom.gmail.authenticate", return_value="user@example.com") as mock_auth:
                                        code = cli_module.setup_cmd(args)

            output = stdout.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("Found OAuth client credentials at", output)
            self.assertIn("Gmail authentication completed successfully", output)
            mock_auth.assert_called_once()


if __name__ == "__main__":
    unittest.main()
