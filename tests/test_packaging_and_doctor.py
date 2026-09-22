"""Tests for packaging modernization, optional Gmail dependencies, auto-initialization, and doctor command."""

import argparse
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from Mailroom import app as app_module
from Mailroom import cli as cli_module
from Mailroom import config as config_module
from Mailroom import db as db_module
from Mailroom import gmail as gmail_module


class TestOptionalGmailDependencies(unittest.TestCase):
    """Test optional Gmail dependency checks, imports, and graceful degradation."""

    def test_check_gmail_dependencies_when_present(self):
        """When all optional packages are installed, check_gmail_dependencies returns (True, [])."""
        ok, missing = gmail_module.check_gmail_dependencies()
        self.assertTrue(ok)
        self.assertEqual(missing, [])

    def test_check_gmail_dependencies_when_keyring_missing(self):
        """When keyring is missing, check_gmail_dependencies returns (False, ['keyring'])."""
        with patch.dict("sys.modules", {"keyring": None}):
            ok, missing = gmail_module.check_gmail_dependencies()
            self.assertFalse(ok)
            self.assertIn("keyring", missing)

    def test_ensure_gmail_dependencies_raises_actionable_error(self):
        """ensure_gmail_dependencies raises GmailError with install instructions when packages are missing."""
        with patch.object(gmail_module, "check_gmail_dependencies", return_value=(False, ["keyring", "google-api-python-client"])):
            with self.assertRaises(gmail_module.GmailError) as cm:
                gmail_module.ensure_gmail_dependencies()
            self.assertIn("keyring, google-api-python-client", str(cm.exception))
            self.assertIn("mailroom[gmail]", str(cm.exception))

    def test_credential_helpers_require_dependencies(self):
        """Credential helper functions call ensure_gmail_dependencies."""
        with patch.object(gmail_module, "ensure_gmail_dependencies", side_effect=gmail_module.GmailError("Deps missing")):
            with self.assertRaises(gmail_module.GmailError):
                gmail_module._store_credentials("test@example.com", "{}")
            with self.assertRaises(gmail_module.GmailError):
                gmail_module._load_credentials("test@example.com")
            with self.assertRaises(gmail_module.GmailError):
                gmail_module._delete_credentials("test@example.com")

    def test_auth_and_get_service_require_dependencies(self):
        """authenticate and get_gmail_service call ensure_gmail_dependencies."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = config_module.Config(str(Path(tmpdir) / "config.json"))
            with patch.object(gmail_module, "ensure_gmail_dependencies", side_effect=gmail_module.GmailError("Deps missing")):
                with self.assertRaises(gmail_module.GmailError):
                    gmail_module.authenticate(cfg)
                with self.assertRaises(gmail_module.GmailError):
                    gmail_module.get_gmail_service(cfg)

    def test_gmail_cli_commands_exit_cleanly_when_deps_missing(self):
        """Gmail CLI commands exit with code 1 and helpful stderr message when dependencies are missing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = config_module.Config(str(Path(tmpdir) / "config.json"))
            args = argparse.Namespace(
                reauth=False,
                apply=False,
                message_id=None,
                limit=None,
                query=None,
                classify=False,
                suggest_labels=False,
            )

            stderr = io.StringIO()
            with patch("sys.stderr", stderr):
                with patch.object(
                    gmail_module,
                    "ensure_gmail_dependencies",
                    side_effect=gmail_module.GmailError("Install mailroom[gmail]"),
                ):
                    # auth
                    code_auth = cli_module.auth_cmd(args, cfg)
                    self.assertEqual(code_auth, 1)
                    self.assertIn("Install mailroom[gmail]", stderr.getvalue())

                    # scan
                    code_scan = cli_module.scan_cmd(args, cfg)
                    self.assertEqual(code_scan, 1)
                    self.assertIn("Gmail error: Install mailroom[gmail]", stderr.getvalue())
                    self.assertNotIn("Run: python -m Mailroom auth", stderr.getvalue())

                    # apply-labels dry run with no targets returns 0
                    code_dry = cli_module.apply_labels_cmd(args, cfg)
                    self.assertEqual(code_dry, 0)

                    # apply-labels with --apply checks dependencies and exits 1
                    args.apply = True
                    db_obj = db_module.DB(cfg.database_path)
                    try:
                        db_obj.get_or_create_account("test@example.com")
                        db_obj.upsert_message(
                            account_id="test@example.com",
                            decoded={
                                "gmail_message_id": "msg1",
                                "thread_id": "th1",
                                "sender": "Sender <s@example.com>",
                                "subject": "Subject",
                                "received_at": None,
                                "body_preview": "Body",
                                "gmail_labels": ["INBOX"],
                            },
                        )
                        msg = db_obj.get_message_by_gmail_id("test@example.com", "msg1")
                        db_obj.save_decision(
                            account_id="test@example.com",
                            message_id=msg["id"],
                            status="accepted",
                            label_ids=["Type/Newsletter"],
                        )
                    finally:
                        db_obj.close()

                    code_apply = cli_module.apply_labels_cmd(args, cfg)
                    self.assertEqual(code_apply, 1)

                    # sync-labels
                    code_sync = cli_module.sync_labels_cmd(args, cfg)
                    self.assertEqual(code_sync, 1)


class TestZeroFrictionAutoInit(unittest.TestCase):
    """Test automatic configuration and database creation on first run."""

    def test_run_review_server_auto_initializes_db_if_missing(self):
        """run_review_server initializes Mailroom.db when it does not exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "config.json"
            cfg = config_module.Config(str(cfg_path))
            db_path = Path(cfg.database_path)

            self.assertFalse(db_path.exists())

            fake_app = MagicMock()
            with patch.object(app_module, "create_app", return_value=fake_app):
                app_module.run_review_server(cfg, host="127.0.0.1", port=5000)

            self.assertTrue(db_path.exists())
            # Verify database has schema initialized
            db_obj = db_module.DB(str(db_path))
            try:
                row = db_obj.conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
                self.assertGreaterEqual(row[0], 5)
            finally:
                db_obj.close()

    def test_classify_cmd_auto_initializes_db_if_missing(self):
        """classify_cmd auto-initializes the database when run against a fresh config."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "config.json"
            cfg = config_module.Config(str(cfg_path))
            db_path = Path(cfg.database_path)

            self.assertFalse(db_path.exists())

            args = argparse.Namespace(
                message_id=None,
                model_endpoint=None,
                limit=10,
                reclassify=False,
                offset=0,
            )
            # classify with no cached messages returns 0
            code = cli_module.classify_cmd(args, cfg)
            self.assertEqual(code, 0)
            self.assertTrue(db_path.exists())


class TestDoctorCommand(unittest.TestCase):
    """Test mailroom doctor diagnostic output and status checks."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.tmpdir.name) / "config.json"
        self.cfg = config_module.Config(str(self.config_path))
        # Ensure database is initialized for doctor tests
        self.db_obj = db_module.DB(self.cfg.database_path)
        self.db_obj.close()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_doctor_command_success_with_healthy_mock_backend(self):
        """mailroom doctor succeeds and displays [✓] when environment and backend are healthy."""
        args = argparse.Namespace(strict=False)
        stdout = io.StringIO()

        mock_probe = {
            "endpoint": self.cfg.model_endpoint,
            "model_id": self.cfg.model_id,
            "status": "ready",
            "provider": "tabby",
        }

        with patch("sys.stdout", stdout):
            with patch("Mailroom.classification.probe_model_endpoint", return_value=mock_probe):
                code = cli_module.doctor_cmd(args, self.cfg)

        output = stdout.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("Mailroom Environment Doctor", output)
        self.assertIn("[✓] Python:", output)
        self.assertIn("[✓] Configuration:", output)
        self.assertIn("[✓] Database:", output)
        self.assertIn("[✓] Backend connectivity:", output)
        self.assertIn("All checks passed. System is ready.", output)

    def test_doctor_command_ollama_model_presence(self):
        """mailroom doctor verifies model presence in Ollama available_models."""
        args = argparse.Namespace(strict=False)
        self.cfg._config["model"]["provider"] = "ollama"
        self.cfg._config["model"]["id"] = "qwen2.5:7b"

        # 1. Model present
        stdout_ok = io.StringIO()
        mock_probe_ok = {
            "endpoint": self.cfg.model_endpoint,
            "model_id": "qwen2.5:7b",
            "status": "ready",
            "provider": "ollama",
            "available_models": ["qwen2.5:7b", "llama3:latest"],
        }
        with patch("sys.stdout", stdout_ok):
            with patch("Mailroom.classification.probe_model_endpoint", return_value=mock_probe_ok):
                code = cli_module.doctor_cmd(args, self.cfg)
        self.assertEqual(code, 0)
        self.assertIn("[✓] Model presence: 'qwen2.5:7b' found in Ollama local library", stdout_ok.getvalue())

        # 2. Model missing
        stdout_missing = io.StringIO()
        mock_probe_missing = {
            "endpoint": self.cfg.model_endpoint,
            "model_id": "other:3b",
            "status": "ready",
            "provider": "ollama",
            "available_models": ["other:3b"],
        }
        with patch("sys.stdout", stdout_missing):
            with patch("Mailroom.classification.probe_model_endpoint", return_value=mock_probe_missing):
                code = cli_module.doctor_cmd(args, self.cfg)
        self.assertEqual(code, 0)
        self.assertIn("[!] Model presence: 'qwen2.5:7b' not found in Ollama local models", stdout_missing.getvalue())
        self.assertIn("ollama pull qwen2.5:7b", stdout_missing.getvalue())

    def test_doctor_command_warns_when_backend_unreachable(self):
        """mailroom doctor gracefully warns when backend is offline."""
        stdout = io.StringIO()
        args = argparse.Namespace(strict=False)

        with patch("sys.stdout", stdout):
            with patch("Mailroom.classification.probe_model_endpoint", side_effect=Exception("Connection refused")):
                code = cli_module.doctor_cmd(args, self.cfg)

        output = stdout.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("[!] Backend connectivity: unable to connect", output)
        self.assertIn("Functional with warnings", output)

        # In strict mode, offline backend returns 1
        stdout_strict = io.StringIO()
        args_strict = argparse.Namespace(strict=True)
        with patch("sys.stdout", stdout_strict):
            with patch("Mailroom.classification.probe_model_endpoint", side_effect=Exception("Connection refused")):
                code_strict = cli_module.doctor_cmd(args_strict, self.cfg)
        self.assertEqual(code_strict, 1)

    def test_doctor_command_flags_non_loopback_endpoint(self):
        """mailroom doctor detects and flags non-loopback endpoints."""
        stdout = io.StringIO()
        args = argparse.Namespace(strict=False)
        self.cfg._config["model"]["endpoint"] = "http://remote-api.com/v1"

        with patch("sys.stdout", stdout):
            code = cli_module.doctor_cmd(args, self.cfg)

        output = stdout.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("is not a loopback URL", output)

    def test_doctor_command_reports_uninitialized_database(self):
        """mailroom doctor accurately notes if database has not been initialized yet."""
        with tempfile.TemporaryDirectory() as empty_dir:
            cfg = config_module.Config(str(Path(empty_dir) / "config.json"))
            args = argparse.Namespace(strict=False)
            stdout = io.StringIO()

            mock_probe = {
                "endpoint": cfg.model_endpoint,
                "model_id": cfg.model_id,
                "status": "ready",
                "provider": "tabby",
            }
            with patch("sys.stdout", stdout):
                with patch("Mailroom.classification.probe_model_endpoint", return_value=mock_probe):
                    code = cli_module.doctor_cmd(args, cfg)

            output = stdout.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("[i] Database:", output)
            self.assertIn("not created yet; will be auto-created", output)

    def test_doctor_command_detects_outdated_schema(self):
        """mailroom doctor detects outdated schema without auto-migrating it."""
        import sqlite3
        with tempfile.TemporaryDirectory() as empty_dir:
            cfg = config_module.Config(str(Path(empty_dir) / "config.json"))
            db_file = Path(cfg.database_path)
            conn = sqlite3.connect(str(db_file))
            conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY);")
            conn.execute("INSERT INTO schema_version (version) VALUES (3);")
            conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY);")
            conn.execute("CREATE TABLE decisions (id INTEGER PRIMARY KEY, status TEXT);")
            conn.commit()
            conn.close()

            args = argparse.Namespace(strict=False)
            stdout = io.StringIO()
            mock_probe = {
                "endpoint": cfg.model_endpoint,
                "model_id": cfg.model_id,
                "status": "ready",
                "provider": "tabby",
            }
            with patch("sys.stdout", stdout):
                with patch("Mailroom.classification.probe_model_endpoint", return_value=mock_probe):
                    code = cli_module.doctor_cmd(args, cfg)

            output = stdout.getvalue()
            self.assertEqual(code, 1)
            self.assertIn("Schema v3 is outdated; run 'mailroom fix-schema'", output)

            # Ensure the database was NOT mutated by doctor
            conn2 = sqlite3.connect(str(db_file))
            v = conn2.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
            conn2.close()
            self.assertEqual(v, 3)

    def test_doctor_command_warns_when_model_is_unknown(self):
        """mailroom doctor warns when probe returns model 'unknown' on Tabby/OpenAI."""
        args = argparse.Namespace(strict=False)
        stdout = io.StringIO()
        mock_probe = {
            "endpoint": self.cfg.model_endpoint,
            "model_id": "unknown",
            "status": "ready",
            "provider": "tabby",
        }
        with patch("sys.stdout", stdout):
            with patch("Mailroom.classification.probe_model_endpoint", return_value=mock_probe):
                code = cli_module.doctor_cmd(args, self.cfg)
        output = stdout.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("[!] Model status: no model currently active in backend", output)

    def test_doctor_command_reports_optional_gmail_dependencies_missing(self):
        """mailroom doctor reports when optional Gmail dependencies are not installed."""
        stdout = io.StringIO()
        args = argparse.Namespace(strict=False)

        mock_probe = {
            "endpoint": self.cfg.model_endpoint,
            "model_id": self.cfg.model_id,
            "status": "ready",
            "provider": "tabby",
        }
        with patch("sys.stdout", stdout):
            with patch("Mailroom.classification.probe_model_endpoint", return_value=mock_probe):
                with patch("Mailroom.gmail.check_gmail_dependencies", return_value=(False, ["keyring", "google-api-python-client"])):
                    code = cli_module.doctor_cmd(args, self.cfg)

        output = stdout.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("[i] Gmail dependencies: not installed (optional, missing: keyring, google-api-python-client)", output)
        self.assertIn("pip install 'mailroom[gmail]'", output)


if __name__ == "__main__":
    unittest.main()
