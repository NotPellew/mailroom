"""End-to-end tests for guided in-app Gmail connection and label sync."""

import io
import json
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from Mailroom import app as app_module
from Mailroom.config import Config
from Mailroom.db import DB
from Mailroom.gmail import GmailError


class TestWebGmail(unittest.TestCase):
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

    def headers(self, csrf=True, origin="http://127.0.0.1:5000", host=None, content_type=None):
        h = {}
        if origin is not None:
            h["Origin"] = origin
        if csrf:
            h["X-CSRF-Token"] = self.csrf
        if host is not None:
            h["Host"] = host
        if content_type is not None:
            h["Content-Type"] = content_type
        return h

    # --- Status Endpoint Tests ---

    def test_gmail_status_disconnected_default(self):
        res = self.client.get("/api/gmail/status", headers=self.headers())
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertFalse(data["connected"])
        self.assertEqual(data["mode"], "local")
        self.assertIsNone(data["account"])
        self.assertFalse(data["credentials_uploaded"])

    # --- Credentials Upload Tests ---

    def test_gmail_credentials_upload_valid_file(self):
        valid_creds = {
            "installed": {
                "client_id": "test-client-id-12345.apps.googleusercontent.com",
                "client_secret": "test-client-secret-abcde",
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        }
        file_bytes = json.dumps(valid_creds).encode("utf-8")
        data = {
            "file": (io.BytesIO(file_bytes), "credentials.json"),
        }

        res = self.client.post(
            "/api/gmail/credentials",
            data=data,
            headers=self.headers(),
            content_type="multipart/form-data",
        )
        self.assertEqual(res.status_code, 200)
        resp_data = res.get_json()
        self.assertEqual(resp_data["status"], "success")
        self.assertIn("test...apps.googleusercontent.com", resp_data["client_id"])

        # Verify saved file exists with 0o600 permissions
        creds_file = self.config.config_dir / "credentials.json"
        self.assertTrue(creds_file.exists())
        file_mode = stat.S_IMODE(creds_file.stat().st_mode)
        self.assertEqual(file_mode, 0o600)

        # Status now reports credentials_uploaded: True
        status_res = self.client.get("/api/gmail/status", headers=self.headers())
        self.assertEqual(status_res.status_code, 200)
        self.assertTrue(status_res.get_json()["credentials_uploaded"])

    def test_gmail_credentials_upload_valid_json_body(self):
        valid_creds = {
            "installed": {
                "client_id": "98765-token.apps.googleusercontent.com",
                "client_secret": "super-secret-key",
            }
        }
        res = self.client.post(
            "/api/gmail/credentials",
            json=valid_creds,
            headers=self.headers(),
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["status"], "success")

    def test_gmail_credentials_rejects_oversized_file(self):
        large_bytes = b"x" * (300 * 1024)  # 300 KB > 256 KB
        data = {
            "file": (io.BytesIO(large_bytes), "credentials.json"),
        }
        res = self.client.post(
            "/api/gmail/credentials",
            data=data,
            headers=self.headers(),
            content_type="multipart/form-data",
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("exceeds maximum size", res.get_json()["error"])

    def test_gmail_credentials_rejects_malformed_json(self):
        data = {
            "file": (io.BytesIO(b"not a valid json {{{{"), "credentials.json"),
        }
        res = self.client.post(
            "/api/gmail/credentials",
            data=data,
            headers=self.headers(),
            content_type="multipart/form-data",
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("Invalid JSON", res.get_json()["error"])

    def test_gmail_credentials_rejects_web_application_type(self):
        web_creds = {
            "web": {
                "client_id": "web-id.apps.googleusercontent.com",
                "client_secret": "secret",
            }
        }
        res = self.client.post(
            "/api/gmail/credentials",
            json=web_creds,
            headers=self.headers(),
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("Desktop app", res.get_json()["error"])

    def test_gmail_credentials_rejects_missing_installed(self):
        res = self.client.post(
            "/api/gmail/credentials",
            json={"other": {}},
            headers=self.headers(),
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("missing 'installed'", res.get_json()["error"])

    def test_gmail_credentials_rejects_missing_secret(self):
        res = self.client.post(
            "/api/gmail/credentials",
            json={"installed": {"client_id": "test-id"}},
            headers=self.headers(),
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("client_secret", res.get_json()["error"])

    # --- Auth & Disconnect Tests ---

    def test_gmail_auth_requires_credentials(self):
        res = self.client.post("/api/gmail/auth", headers=self.headers())
        self.assertEqual(res.status_code, 400)
        self.assertIn("credentials not uploaded yet", res.get_json()["error"])

    @patch("Mailroom.gmail.check_gmail_dependencies", return_value=(False, ["google-api-python-client"]))
    def test_gmail_auth_requires_dependencies(self, _mock_deps):
        res = self.client.post("/api/gmail/auth", headers=self.headers())
        self.assertEqual(res.status_code, 400)
        self.assertIn("dependencies not installed", res.get_json()["error"])

    @patch("Mailroom.gmail.authenticate")
    @patch("Mailroom.gmail._load_client_secrets", return_value={"installed": {}})
    @patch("Mailroom.gmail.check_gmail_dependencies", return_value=(True, []))
    def test_gmail_auth_success(self, _mock_deps, _mock_secrets, mock_auth):
        mock_auth.return_value = "tester@gmail.com"

        res = self.client.post("/api/gmail/auth", headers=self.headers())
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["status"], "success")
        self.assertEqual(res.get_json()["account"], "tester@gmail.com")

    def test_gmail_auth_concurrency_lock(self):
        import Mailroom.routes as routes_module

        acquired = routes_module._GMAIL_AUTH_LOCK.acquire(blocking=False)
        self.assertTrue(acquired)
        try:
            res = self.client.post("/api/gmail/auth", headers=self.headers())
            self.assertEqual(res.status_code, 409)
            self.assertIn("already in progress", res.get_json()["error"])
        finally:
            routes_module._GMAIL_AUTH_LOCK.release()

    @patch("Mailroom.gmail.clear_stored_auth")
    @patch("Mailroom.gmail._read_last_email", return_value="tester@gmail.com")
    def test_gmail_disconnect(self, _mock_email, mock_clear):
        marker_file = self.config.config_dir / ".last_gmail_email"
        marker_file.write_text("tester@gmail.com", encoding="utf-8")
        self.assertTrue(marker_file.exists())

        res = self.client.post("/api/gmail/disconnect", headers=self.headers())
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.get_json()["connected"])

        mock_clear.assert_called_once_with("tester@gmail.com")
        self.assertFalse(marker_file.exists())

    # --- Scan Endpoint Tests ---

    @patch("Mailroom.gmail.fetch_bounded_sample")
    @patch("Mailroom.gmail.get_gmail_service")
    def test_gmail_scan(self, mock_get_svc, mock_fetch):
        mock_svc = MagicMock()
        mock_get_svc.return_value = (mock_svc, "scanner@gmail.com")

        def simulate_fetch(service, query, limit, skip_ids, on_message):
            on_message({
                "gmail_message_id": "gm-101",
                "thread_id": "th-101",
                "subject": "Invoice June",
                "sender": "Billing Corp",
                "sender_email": "billing@corp.com",
                "received_at": "2026-06-01T12:00:00",
                "body_preview": "Your total is $42.00",
                "gmail_labels": ["INBOX"],
                "truncated": False,
                "unsupported_content": False,
                "has_attachments": False,
            })
            return 1, "2026-06-01T12:00:00"

        mock_fetch.side_effect = simulate_fetch

        res = self.client.post(
            "/api/gmail/scan",
            json={"limit": 25},
            headers=self.headers(),
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["new"], 1)
        self.assertEqual(data["account"], "scanner@gmail.com")

        # Verify message stored in DB
        db = DB(self.config.database_path)
        try:
            cached_ids = db.list_cached_gmail_ids("scanner@gmail.com")
            self.assertIn("gm-101", cached_ids)
            msg = db.get_message_by_gmail_id("scanner@gmail.com", "gm-101")
            self.assertIsNotNone(msg)
            self.assertEqual(msg["subject"], "Invoice June")
            self.assertEqual(msg["gmail_message_id"], "gm-101")
        finally:
            db.close()

    # --- Apply Preview & Execution Tests ---

    def test_gmail_apply_preview_and_safety_guarantee(self):
        db = DB(self.config.database_path, allowed_labels=self.config.get_label_ids())
        try:
            account_id = db.get_or_create_account("user@gmail.com", is_local=False)
            msg_id = db.upsert_message(account_id, {
                "gmail_message_id": "gm-201",
                "thread_id": "th-201",
                "subject": "Hosting Bill",
                "sender": "Cloud Host",
                "sender_email": "host@cloud.com",
                "received_at": "2026-06-01T12:00:00",
                "body_preview": "Invoice details",
                "gmail_labels": [],
                "truncated": False,
                "unsupported_content": False,
                "has_attachments": False,
            })
            # Save accepted decision
            db.save_decision(account_id, msg_id, "accepted", ["Type/Receipt"])
        finally:
            db.close()

        res = self.client.get("/api/gmail/apply/preview", headers=self.headers())
        self.assertEqual(res.status_code, 200)
        data = res.get_json()

        self.assertEqual(data["total_messages"], 1)
        self.assertEqual(data["distinct_labels"], ["Type/Receipt"])
        self.assertIn("0 messages will be archived or deleted", data["safety_guarantee"])
        self.assertEqual(len(data["items"]), 1)
        item = data["items"][0]
        self.assertEqual(item["gmail_message_id"], "gm-201")
        self.assertEqual(item["subject"], "Hosting Bill")
        self.assertEqual(item["labels_to_add"], ["Type/Receipt"])
        self.assertEqual(item["labels_to_remove"], [])

    @patch("Mailroom.gmail.modify_message_labels")
    @patch("Mailroom.gmail.ensure_label")
    @patch("Mailroom.gmail.list_user_labels")
    @patch("Mailroom.gmail.get_gmail_service")
    def test_gmail_apply_execution(self, mock_get_svc, mock_list_labels, mock_ensure, mock_modify):
        mock_svc = MagicMock()
        mock_get_svc.return_value = (mock_svc, "user@gmail.com")
        mock_list_labels.return_value = {}
        mock_ensure.return_value = "Label_Receipt_123"

        db = DB(self.config.database_path, allowed_labels=self.config.get_label_ids())
        try:
            account_id = db.get_or_create_account("user@gmail.com", is_local=False)
            msg_id = db.upsert_message(account_id, {
                "gmail_message_id": "gm-301",
                "thread_id": "th-301",
                "subject": "Monthly Statement",
                "sender": "Bank",
                "sender_email": "bank@example.com",
                "received_at": "2026-06-01T12:00:00",
                "body_preview": "Statement attached",
                "gmail_labels": [],
                "truncated": False,
                "unsupported_content": False,
                "has_attachments": False,
            })
            db.save_decision(account_id, msg_id, "accepted", ["Type/Receipt"])
        finally:
            db.close()

        res = self.client.post("/api/gmail/apply", headers=self.headers())
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["applied"], 1)
        self.assertEqual(data["failed"], 0)

        # Verify modify_message_labels called with correct label IDs
        mock_modify.assert_called_once_with(
            mock_svc,
            "gm-301",
            add_label_ids=["Label_Receipt_123"],
            remove_label_ids=[],
        )

        # Verify applied label snapshot recorded in DB
        db = DB(self.config.database_path)
        try:
            snaps = db.list_applied_labels()
            self.assertEqual(len(snaps), 1)
            self.assertEqual(snaps[0]["label_ids"], ["Type/Receipt"])
        finally:
            db.close()

    def test_gmail_apply_empty_plan(self):
        res = self.client.post("/api/gmail/apply", headers=self.headers())
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["applied"], 0)

    # --- Sync Labels Tests ---

    @patch("Mailroom.gmail.fetch_message_label_ids")
    @patch("Mailroom.gmail.list_user_labels")
    @patch("Mailroom.gmail.get_gmail_service")
    def test_gmail_sync_labels(self, mock_get_svc, mock_list_labels, mock_fetch_ids):
        mock_svc = MagicMock()
        mock_get_svc.return_value = (mock_svc, "user@gmail.com")
        mock_list_labels.return_value = {
            "Type/Receipt": "lid-receipt",
            "Type/Newsletter": "lid-news",
        }
        # Simulate that in Gmail, the user changed the label from Type/Receipt to Type/Newsletter
        mock_fetch_ids.return_value = ["lid-news"]

        db = DB(self.config.database_path, allowed_labels=self.config.get_label_ids())
        try:
            account_id = db.get_or_create_account("user@gmail.com", is_local=False)
            msg_id = db.upsert_message(account_id, {
                "gmail_message_id": "gm-401",
                "thread_id": "th-401",
                "subject": "Weekly Tech Digest",
                "sender": "News Org",
                "sender_email": "news@example.com",
                "received_at": "2026-06-01T12:00:00",
                "body_preview": "digest",
                "gmail_labels": [],
                "truncated": False,
                "unsupported_content": False,
                "has_attachments": False,
            })
            db.save_decision(account_id, msg_id, "accepted", ["Type/Receipt"])
            db.record_applied_labels(account_id, msg_id, ["Type/Receipt"])
        finally:
            db.close()

        res = self.client.post("/api/gmail/sync-labels", headers=self.headers())
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["corrected"], 1)
        self.assertEqual(data["unchanged"], 0)

        # Verify DB updated with corrected decision
        db = DB(self.config.database_path)
        try:
            decision = db.get_decision(msg_id)
            self.assertIsNotNone(decision)
            self.assertEqual(decision["status"], "corrected")
            self.assertEqual(decision["label_ids_parsed"], ["Type/Newsletter"])
        finally:
            db.close()

    # --- Security & CSRF Tests ---

    def test_csrf_protection_on_mutating_endpoints(self):
        endpoints = [
            "/api/gmail/credentials",
            "/api/gmail/auth",
            "/api/gmail/disconnect",
            "/api/gmail/scan",
            "/api/gmail/apply",
            "/api/gmail/sync-labels",
        ]
        for ep in endpoints:
            res = self.client.post(ep, headers=self.headers(csrf=False))
            self.assertEqual(res.status_code, 403, f"Endpoint {ep} did not enforce CSRF")

    def test_host_header_enforcement(self):
        endpoints = [
            ("GET", "/api/gmail/status"),
            ("GET", "/api/gmail/apply/preview"),
            ("POST", "/api/gmail/credentials"),
            ("POST", "/api/gmail/auth"),
            ("POST", "/api/gmail/disconnect"),
            ("POST", "/api/gmail/scan"),
            ("POST", "/api/gmail/apply"),
            ("POST", "/api/gmail/sync-labels"),
        ]
        for method, ep in endpoints:
            if method == "GET":
                res = self.client.get(ep, headers=self.headers(host="evil.com"))
            else:
                res = self.client.post(ep, headers=self.headers(host="evil.com"))
            self.assertEqual(res.status_code, 400, f"Endpoint {ep} did not reject hostile Host header")


if __name__ == "__main__":
    unittest.main()
