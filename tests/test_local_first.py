"""Local-first acceptance tests for issue #5.

The core journey (ingest local `.eml` -> review -> export) must work with no
Gmail account, no OAuth credentials, and the optional Gmail dependencies absent.
Gmail writes stay behind the explicit ``apply-labels --apply`` gate.
"""

import argparse
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from EmailMan import app as app_module
from EmailMan import cli as cli_module
from EmailMan import config as config_module
from EmailMan import db as db_module
from EmailMan import gmail as gmail_module
from EmailMan import ingest as ingest_module

SAMPLE_EML = b"""From: Shop <noreply@shop.example>
To: user@example.com
Subject: Your order confirmation #123
Date: Mon, 21 Sep 2026 10:00:00 +0000
Message-ID: <order-123@shop.example>
Content-Type: text/plain; charset="utf-8"

Thanks for your purchase. Total $19.99.
"""

# Optional Gmail dependencies that must not be required for the local path.
GMAIL_MODULES = (
    "keyring",
    "google",
    "googleapiclient",
    "google.oauth2",
    "google.auth",
    "google_auth_httplib2",
    "google_auth_oauthlib",
)


class LocalFirstTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.temp_dir, "config.json")
        self.config = config_module.Config(self.config_path)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _without_gmail_deps(self):
        return patch.dict(sys.modules, {name: None for name in GMAIL_MODULES})


class TestLocalFirstPipeline(LocalFirstTestBase):
    def test_full_local_pipeline_without_gmail_dependencies(self):
        mail_dir = Path(self.temp_dir) / "mail"
        mail_dir.mkdir()
        (mail_dir / "sample.eml").write_bytes(SAMPLE_EML)

        with self._without_gmail_deps():
            ok, missing = gmail_module.check_gmail_dependencies()
            self.assertFalse(ok)
            self.assertIn("keyring", missing)

            db_obj = db_module.DB(self.config.database_path)
            try:
                result = ingest_module.ingest_path(db_obj, mail_dir)
            finally:
                db_obj.close()
            self.assertEqual(result.ingested, 1)

            client = app_module.create_app(self.config).test_client()
            self.assertEqual(client.get("/").status_code, 200)

            status = client.get("/api/status").get_json()
            self.assertEqual(status["mode"], "local")
            self.assertEqual(status["gmail_accounts"], 0)
            self.assertGreaterEqual(status["local_accounts"], 1)

            self.assertEqual(client.get("/api/review").get_json()["total"], 1)
            self.assertEqual(client.get("/api/export?format=csv").status_code, 200)
            self.assertEqual(client.get("/api/export?format=json").status_code, 200)

    def test_status_defaults_to_local_without_database(self):
        from EmailMan.routes import get_status

        status = get_status(os.path.join(self.temp_dir, "missing.db"))
        self.assertFalse(status["db_initialized"])
        self.assertEqual(status["mode"], "local")
        self.assertEqual(status["gmail_accounts"], 0)
        self.assertEqual(status["local_accounts"], 0)

    def test_status_reports_gmail_mode_with_gmail_account(self):
        db_obj = db_module.DB(self.config.database_path)
        try:
            db_obj.get_or_create_account("user@gmail.com")
        finally:
            db_obj.close()

        status = app_module.create_app(self.config).test_client().get("/api/status").get_json()
        self.assertEqual(status["mode"], "gmail")
        self.assertEqual(status["gmail_accounts"], 1)
        self.assertEqual(status["local_accounts"], 0)

    def test_template_exposes_local_mode_banner(self):
        template_path = Path(__file__).resolve().parent.parent / "EmailMan" / "templates" / "index.html"
        html = template_path.read_text(encoding="utf-8")
        self.assertIn("mode-banner", html)
        self.assertIn("Local only", html)
        self.assertIn("data.mode", html)


class TestCliLocalFirstMessaging(LocalFirstTestBase):
    def test_create_config_next_steps_are_local_first(self):
        target = os.path.join(self.temp_dir, "nested", "config.json")
        args = argparse.Namespace(config=target, profile="tabby")

        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            code = cli_module.create_config_cmd(args)
        self.assertEqual(code, 0)

        out = stdout.getvalue()
        local_idx = out.index("local-first")
        gmail_idx = out.index("Optional: mirror labels in Gmail")
        self.assertLess(local_idx, gmail_idx)

        local_section = out[local_idx:gmail_idx]
        self.assertIn("init-db", local_section)
        self.assertIn("ingest", local_section)
        self.assertNotIn("auth", local_section)

    def test_help_groups_local_workflow_before_optional_gmail(self):
        help_text = cli_module.create_cli_parser().format_help()
        local_idx = help_text.index("Local-first workflow (no Gmail account needed)")
        gmail_idx = help_text.index(
            "Optional Gmail sync (needs OAuth credentials and the [gmail] extra)"
        )
        self.assertLess(local_idx, gmail_idx)

        local_section = help_text[local_idx:gmail_idx]
        self.assertIn("ingest", local_section)
        self.assertNotIn("auth", local_section)

    def test_scan_missing_credentials_suggests_local_ingest(self):
        args = cli_module.create_cli_parser().parse_args(["--config", self.config_path, "scan"])
        stderr = io.StringIO()
        with patch.object(
            gmail_module,
            "get_gmail_service",
            side_effect=gmail_module.GmailError("Missing credentials for stored account"),
        ), patch("sys.stderr", stderr):
            code = cli_module.scan_cmd(args, self.config)

        self.assertEqual(code, 1)
        err = stderr.getvalue()
        self.assertIn("python -m EmailMan auth", err)
        self.assertIn("ingest", err)

    def test_apply_labels_dry_run_needs_no_gmail_dependencies(self):
        db_obj = db_module.DB(self.config.database_path)
        try:
            account = db_obj.get_or_create_account("user@gmail.com")
            message_id = db_obj.upsert_message(
                account,
                {
                    "gmail_message_id": "gm-1",
                    "thread_id": "th-1",
                    "sender": "Sender <s@example.com>",
                    "subject": "Subject",
                    "received_at": None,
                    "body_preview": "Body",
                    "gmail_labels": ["INBOX"],
                    "truncated": False,
                    "unsupported_content": False,
                    "has_attachments": False,
                },
            )
            db_obj.save_decision(account, message_id, "accepted", ["Type/Newsletter"])
        finally:
            db_obj.close()

        args = cli_module.create_cli_parser().parse_args(
            ["--config", self.config_path, "apply-labels"]
        )
        with self._without_gmail_deps(), patch.object(
            gmail_module, "modify_message_labels"
        ) as mock_modify:
            code = cli_module.apply_labels_cmd(args, self.config)

        self.assertEqual(code, 0)
        mock_modify.assert_not_called()
        self.assertTrue(
            (Path(self.config.config_dir) / "apply-labels-plan.csv").exists()
        )


if __name__ == "__main__":
    unittest.main()
