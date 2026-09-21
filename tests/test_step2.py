"""Step 2 acceptance tests: read-only Gmail adapter and safe MIME handling.

These tests are synthetic and do not require live Gmail credentials.
"""

import unittest
import json
import base64
import tempfile
import os
import sqlite3
import io
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

from EmailMan import gmail as gmail_module
from EmailMan import db as db_module
from EmailMan import config as config_module
from EmailMan import cli as cli_module
from EmailMan import app as app_module


def _b64url(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii").rstrip("=")


class TestGmailDecodeSynthetic(unittest.TestCase):
    """Synthetic MIME / payload decoding tests for required cases."""

    def test_plain_text_body(self):
        payload = {
            "mimeType": "text/plain",
            "headers": [{"name": "Subject", "value": "Plain"}, {"name": "From", "value": "a@b.com"}],
            "body": {"data": _b64url("Hello plain world")},
        }
        msg = {"id": "m1", "threadId": "t1", "payload": payload, "labelIds": ["INBOX"]}
        d = gmail_module.decode_gmail_message(msg)
        self.assertEqual(d["gmail_message_id"], "m1")
        self.assertEqual(d["thread_id"], "t1")
        self.assertIn("Hello plain", d["body_preview"])
        self.assertFalse(d["truncated"])
        self.assertFalse(d["unsupported_content"])
        self.assertEqual(d["gmail_labels"], ["INBOX"])

    def test_multipart_plain_preferred_over_html(self):
        payload = {
            "mimeType": "multipart/alternative",
            "headers": [{"name": "Subject", "value": "Multi"}, {"name": "From", "value": "x@y"}],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64url("Plain part here")}},
                {"mimeType": "text/html", "body": {"data": _b64url("<b>HTML</b>")}},
            ],
        }
        d = gmail_module.decode_gmail_message({"id": "m2", "threadId": "t2", "payload": payload})
        self.assertIn("Plain part", d["body_preview"])
        self.assertNotIn("<b>", d["body_preview"])

    def test_html_only_converted_to_inert_text(self):
        payload = {
            "mimeType": "text/html",
            "headers": [{"name": "Subject", "value": "HTML"}, {"name": "From", "value": "h@h"}],
            "body": {"data": _b64url("<html><body><p>Hi</p><script>bad()</script></body></html>")},
        }
        d = gmail_module.decode_gmail_message({"id": "m3", "payload": payload})
        self.assertIn("Hi", d["body_preview"])
        self.assertNotIn("<script", d["body_preview"].lower())
        self.assertNotIn("bad()", d["body_preview"])

    def test_attachment_skipped_and_flagged(self):
        payload = {
            "mimeType": "multipart/mixed",
            "headers": [{"name": "Subject", "value": "Attach"}, {"name": "From", "value": "a@a"}],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64url("See attachment")}},
                {
                    "mimeType": "application/pdf",
                    "filename": "doc.pdf",
                    "body": {"attachmentId": "att1"},
                    "headers": [{"name": "Content-Disposition", "value": "attachment; filename=doc.pdf"}],
                },
            ],
        }
        d = gmail_module.decode_gmail_message({"id": "m4", "payload": payload})
        self.assertIn("See attachment", d["body_preview"])
        self.assertTrue(d["has_attachments"])

    def test_malformed_and_empty_payload(self):
        d = gmail_module.decode_gmail_message(None)
        self.assertIsNone(d["gmail_message_id"])
        self.assertTrue(d["unsupported_content"] or d["body_preview"] == "")

        d2 = gmail_module.decode_gmail_message({"id": "bad", "payload": {"mimeType": "application/octet-stream"}})
        self.assertEqual(d2["gmail_message_id"], "bad")
        # No usable text body
        self.assertTrue(d2["unsupported_content"] or len(d2["body_preview"]) == 0)

    def test_malformed_base64_is_flagged_not_garbled(self):
        payload = {
            "mimeType": "text/plain",
            "headers": [{"name": "Subject", "value": "Bad"}, {"name": "From", "value": "a@b"}],
            "body": {"data": "!!!not-valid-base64!!!"},
        }
        d = gmail_module.decode_gmail_message({"id": "m-badb64", "payload": payload})
        self.assertEqual(d["body_preview"], "")
        self.assertTrue(d["unsupported_content"])

    def test_malformed_payload_shapes_do_not_raise(self):
        shapes = [
            {"id": "shape-1", "payload": "oops"},
            {"id": "shape-2", "payload": {"mimeType": "multipart/mixed", "parts": "nope"}},
            {"id": "shape-3", "payload": {"mimeType": "text/plain", "body": "nope"}},
            {
                "id": "shape-4",
                "payload": {"mimeType": "text/plain", "headers": "nope", "body": {"data": _b64url("ok")}},
            },
        ]
        for shape in shapes:
            d = gmail_module.decode_gmail_message(shape)
            self.assertIsInstance(d, dict)
            self.assertIsInstance(d["body_preview"], str)
            if shape["id"] != "shape-4":
                # Structurally malformed payloads are flagged, not crashed on.
                self.assertTrue(d["unsupported_content"])

    def test_truncation_flag(self):
        long_text = "X" * (gmail_module.BODY_PREVIEW_LIMIT + 100)
        payload = {
            "mimeType": "text/plain",
            "headers": [{"name": "Subject", "value": "Long"}, {"name": "From", "value": "l@l"}],
            "body": {"data": _b64url(long_text)},
        }
        d = gmail_module.decode_gmail_message({"id": "m5", "payload": payload})
        self.assertTrue(d["truncated"])
        self.assertEqual(len(d["body_preview"]), gmail_module.BODY_PREVIEW_LIMIT)

    def test_readonly_scope_constant(self):
        self.assertIn("readonly", gmail_module.GMAIL_READONLY_SCOPE)
        self.assertNotIn("modify", gmail_module.GMAIL_READONLY_SCOPE.lower())
        self.assertNotIn("insert", gmail_module.GMAIL_READONLY_SCOPE.lower())
        self.assertNotIn("labels", gmail_module.GMAIL_READONLY_SCOPE.lower())  # not using modify labels scope

    def test_only_label_mutations_in_source(self):
        """Static inspection: only label create/modify are present; no destructive verbs."""
        import inspect

        src = inspect.getsource(gmail_module)
        forbidden = [
            ".trash(",
            ".untrash(",
            ".delete(",
            ".batchDelete",
            ".send(",
            ".insert(",
            ".drafts(",
            "gmail.compose",
            "gmail.send",
            "gmail.full",
            "https://mail.google.com/",
        ]
        for token in forbidden:
            self.assertNotIn(token, src)
        # The only writes are label creation and message label changes.
        self.assertIn(".labels()", src)
        self.assertIn(".create(", src)
        self.assertIn(".modify(", src)


class TestUpsertAndDecisionPreservation(unittest.TestCase):
    """Re-scan must not duplicate and must preserve human decisions."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "EmailMan.db")
        self.db = db_module.DB(self.db_path)
        # seed account
        self.account_id = self.db.get_or_create_account("user@example.com")

    def tearDown(self):
        self.db.close()
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_upsert_idempotent_and_preserves_decisions(self):
        decoded = {
            "gmail_message_id": "gm-123",
            "thread_id": "th-1",
            "subject": "Hello",
            "sender": "Alice",
            "sender_email": "alice@example.com",
            "received_at": "2024-01-01T00:00:00+00:00",
            "body_preview": "Body one",
            "gmail_labels": ["INBOX"],
            "truncated": False,
            "unsupported_content": False,
            "has_attachments": False,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

        # First insert
        mid1 = self.db.upsert_message(self.account_id, decoded)
        self.assertTrue(mid1)

        # Add a human decision
        cur = self.db.conn.cursor()
        cur.execute(
            """
            INSERT INTO decisions (id, message_id, account_id, status, label_ids)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("dec-1", mid1, self.account_id, "accepted", '["Type/Receipt"]'),
        )
        self.db.conn.commit()

        # Re-scan with updated body (should not duplicate, preserve decision)
        decoded2 = dict(decoded)
        decoded2["body_preview"] = "Body two updated"
        decoded2["fetched_at"] = datetime.now(timezone.utc).isoformat()
        mid2 = self.db.upsert_message(self.account_id, decoded2)
        self.assertEqual(mid1, mid2)

        # Verify single row and decision still present
        cur.execute("SELECT COUNT(*) as c FROM messages WHERE gmail_message_id = ?", ("gm-123",))
        self.assertEqual(cur.fetchone()["c"], 1)
        cur.execute("SELECT status, label_ids FROM decisions WHERE message_id = ?", (mid1,))
        row = cur.fetchone()
        self.assertEqual(row["status"], "accepted")
        self.assertEqual(row["label_ids"], '["Type/Receipt"]')

        # Body updated
        cur.execute("SELECT body_preview FROM messages WHERE id = ?", (mid1,))
        self.assertEqual(cur.fetchone()["body_preview"], "Body two updated")

    def test_repeat_scan_does_not_create_duplicates(self):
        base = {
            "gmail_message_id": "gm-dup",
            "thread_id": "th-d",
            "subject": "Dup test",
            "sender": "D",
            "sender_email": "d@d",
            "received_at": None,
            "body_preview": "B",
            "gmail_labels": [],
            "truncated": False,
            "unsupported_content": False,
            "has_attachments": False,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        id1 = self.db.upsert_message(self.account_id, base)
        id2 = self.db.upsert_message(self.account_id, base)
        self.assertEqual(id1, id2)
        cur = self.db.conn.cursor()
        cur.execute("SELECT COUNT(*) as c FROM messages WHERE gmail_message_id = ?", ("gm-dup",))
        self.assertEqual(cur.fetchone()["c"], 1)

    def test_enforce_body_cache_expiry(self):
        old_time = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        recent = datetime.now(timezone.utc).isoformat()

        cur = self.db.conn.cursor()
        cur.execute(
            """
            INSERT INTO messages (id, account_id, gmail_message_id, body_preview, fetched_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("old1", self.account_id, "gm-old", "secret old", old_time),
        )
        cur.execute(
            """
            INSERT INTO messages (id, account_id, gmail_message_id, body_preview, fetched_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("new1", self.account_id, "gm-new", "recent", recent),
        )
        self.db.conn.commit()

        n = self.db.enforce_body_cache_expiry(days=7)
        self.assertGreaterEqual(n, 1)

        cur.execute("SELECT body_preview FROM messages WHERE id='old1'")
        self.assertIsNone(cur.fetchone()["body_preview"])
        cur.execute("SELECT body_preview FROM messages WHERE id='new1'")
        self.assertEqual(cur.fetchone()["body_preview"], "recent")


class TestGmailModuleStructure(unittest.TestCase):
    """Ensure the module exposes the expected surface: reads plus label writes only."""

    def test_public_api(self):
        self.assertTrue(hasattr(gmail_module, "authenticate"))
        self.assertTrue(hasattr(gmail_module, "get_gmail_service"))
        self.assertTrue(hasattr(gmail_module, "fetch_account_and_labels"))
        self.assertTrue(hasattr(gmail_module, "decode_gmail_message"))
        self.assertTrue(hasattr(gmail_module, "fetch_bounded_sample"))
        self.assertTrue(hasattr(gmail_module, "GMAIL_READONLY_SCOPE"))
        self.assertTrue(hasattr(gmail_module, "GMAIL_MODIFY_SCOPE"))
        self.assertTrue(hasattr(gmail_module, "GMAIL_SCOPES"))
        self.assertTrue(hasattr(gmail_module, "GmailError"))
        # Label write surface
        self.assertTrue(hasattr(gmail_module, "list_user_labels"))
        self.assertTrue(hasattr(gmail_module, "ensure_label"))
        self.assertTrue(hasattr(gmail_module, "modify_message_labels"))
        self.assertTrue(hasattr(gmail_module, "fetch_message_label_ids"))
        self.assertTrue(hasattr(gmail_module, "get_stored_scopes"))

    def test_source_requests_only_modify_scope(self):
        import inspect
        src = inspect.getsource(gmail_module)
        # Requests gmail.modify and nothing broader.
        self.assertIn("gmail.modify", src)
        self.assertNotIn("gmail.full", src)
        self.assertNotIn("gmail.compose", src)
        self.assertNotIn("gmail.send", src)
        self.assertNotIn("https://mail.google.com/", src)


_TOKEN = {
    "token": "access-token",
    "refresh_token": "refresh-token",
    "token_uri": "https://oauth2.googleapis.com/token",
    "client_id": "cid",
    "client_secret": "cs",
    "scopes": [gmail_module.GMAIL_READONLY_SCOPE],
}


def _fake_profile_service(email="user@gmail.com"):
    service = MagicMock()
    service.users.return_value.getProfile.return_value.execute.return_value = {
        "emailAddress": email
    }
    return service


class TestAuthHandshake(unittest.TestCase):
    """Auth must write the last-email marker so scan can load keyring credentials."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.temp_dir, "config.json")
        self.config = config_module.Config(self.config_path)
        self.stored = {}

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _store(self, email, data):
        self.stored[email] = data

    def _load(self, email=None):
        if email and email in self.stored:
            return json.loads(self.stored[email])
        return None

    def test_authenticate_writes_marker_then_get_service_succeeds(self):
        creds = MagicMock()
        creds.to_json.return_value = json.dumps(_TOKEN)
        flow = MagicMock()
        flow.run_local_server.return_value = creds
        service = _fake_profile_service("user@gmail.com")

        with patch.object(gmail_module, "_load_client_secrets", return_value={"installed": {}}), \
             patch.object(gmail_module, "_store_credentials", side_effect=self._store), \
             patch.object(gmail_module, "_load_credentials", side_effect=self._load), \
             patch.object(gmail_module, "_save_refreshed_credentials"), \
             patch("google_auth_oauthlib.flow.InstalledAppFlow") as Flow, \
             patch.object(gmail_module, "_build_gmail_service", return_value=(service, creds)), \
             patch("googleapiclient.discovery.build", return_value=service):
            Flow.from_client_config.return_value = flow

            email = gmail_module.authenticate(self.config, reauth=False)
            self.assertEqual(email, "user@gmail.com")
            marker = self.config.config_dir / gmail_module.LAST_EMAIL_FILENAME
            self.assertTrue(marker.exists())
            self.assertEqual(marker.read_text(encoding="utf-8").strip(), "user@gmail.com")
            self.assertIn("user@gmail.com", self.stored)

            service2, email2 = gmail_module.get_gmail_service(self.config)
            self.assertEqual(email2, "user@gmail.com")
            self.assertIs(service2, service)

    def test_authenticate_reuses_stored_token_without_browser(self):
        marker = self.config.config_dir / gmail_module.LAST_EMAIL_FILENAME
        marker.write_text("user@gmail.com", encoding="utf-8")
        self.stored["user@gmail.com"] = json.dumps(_TOKEN)
        service = _fake_profile_service("user@gmail.com")
        creds = MagicMock()

        with patch.object(gmail_module, "_load_credentials", side_effect=self._load), \
             patch.object(gmail_module, "_save_refreshed_credentials"), \
             patch.object(gmail_module, "_build_gmail_service", return_value=(service, creds)), \
             patch("google_auth_oauthlib.flow.InstalledAppFlow") as Flow:
            email = gmail_module.authenticate(self.config, reauth=False)
            self.assertEqual(email, "user@gmail.com")
            Flow.from_client_config.assert_not_called()

    def test_reauth_forces_browser_flow(self):
        marker = self.config.config_dir / gmail_module.LAST_EMAIL_FILENAME
        marker.write_text("user@gmail.com", encoding="utf-8")
        self.stored["user@gmail.com"] = json.dumps(_TOKEN)
        creds = MagicMock()
        creds.to_json.return_value = json.dumps(_TOKEN)
        flow = MagicMock()
        flow.run_local_server.return_value = creds
        service = _fake_profile_service("user@gmail.com")

        with patch.object(gmail_module, "_load_client_secrets", return_value={"installed": {}}), \
             patch.object(gmail_module, "_store_credentials", side_effect=self._store), \
             patch.object(gmail_module, "_load_credentials", side_effect=self._load), \
             patch("google_auth_oauthlib.flow.InstalledAppFlow") as Flow, \
             patch("googleapiclient.discovery.build", return_value=service):
            Flow.from_client_config.return_value = flow
            email = gmail_module.authenticate(self.config, reauth=True)
            self.assertEqual(email, "user@gmail.com")
            Flow.from_client_config.assert_called_once()
            flow.run_local_server.assert_called_once()


class TestBodySizeTruncation(unittest.TestCase):
    def test_body_size_larger_than_payload_flags_truncated(self):
        payload = {
            "mimeType": "text/plain",
            "headers": [{"name": "Subject", "value": "T"}, {"name": "From", "value": "a@b"}],
            "body": {"data": _b64url("Hello"), "size": 5000},
        }
        d = gmail_module.decode_gmail_message({"id": "m-size", "payload": payload})
        self.assertTrue(d["truncated"])
        self.assertEqual(d["body_preview"], "Hello")

    def test_text_part_with_size_but_no_data_flags_truncated(self):
        payload = {
            "mimeType": "text/plain",
            "headers": [{"name": "Subject", "value": "T"}, {"name": "From", "value": "a@b"}],
            "body": {"size": 12000},
        }
        d = gmail_module.decode_gmail_message({"id": "m-nodata", "payload": payload})
        self.assertTrue(d["truncated"])
        self.assertTrue(d["unsupported_content"])

    def test_huge_encoded_body_is_capped_before_decode(self):
        """Finding 4: a huge base64 part is not fully decoded before trimming."""
        huge = _b64url("A" * (gmail_module.BODY_RAW_DATA_LIMIT * 3))
        self.assertGreater(len(huge), gmail_module.BODY_RAW_DATA_LIMIT)
        payload = {
            "mimeType": "text/html",
            "headers": [{"name": "Subject", "value": "Huge"}, {"name": "From", "value": "a@b"}],
            "body": {"data": huge},
        }
        d = gmail_module.decode_gmail_message({"id": "m-huge", "payload": payload})
        self.assertTrue(d["truncated"])
        self.assertLessEqual(len(d["body_preview"]), gmail_module.BODY_PREVIEW_LIMIT)


class TestAccountMixingAndScanMetadata(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "EmailMan.db")
        self.db = db_module.DB(self.db_path)

    def tearDown(self):
        self.db.close()
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_refuses_second_active_account(self):
        first = self.db.get_or_create_account("user@example.com")
        self.assertEqual(first, "user@example.com")
        with self.assertRaises(db_module.DatabaseError) as cm:
            self.db.get_or_create_account("other@example.com")
        self.assertIn("mix", str(cm.exception).lower())

    def test_same_account_case_insensitive_reuse(self):
        a = self.db.get_or_create_account("user@example.com")
        b = self.db.get_or_create_account("User@Example.com")
        self.assertEqual(a, b)

    def test_record_scan_metadata(self):
        acc = self.db.get_or_create_account("user@example.com")
        catalog = [{"id": "INBOX", "name": "INBOX", "type": "system"}]
        self.db.record_scan_metadata(acc, gmail_module.DEFAULT_GMAIL_QUERY, "2024-01-01T00:00:00+00:00", catalog)
        row = self.db.get_account(acc)
        self.assertEqual(row["last_query"], gmail_module.DEFAULT_GMAIL_QUERY)
        self.assertEqual(row["last_sync"], "2024-01-01T00:00:00+00:00")
        stored = json.loads(row["gmail_label_catalog"])
        self.assertEqual(stored[0]["id"], "INBOX")

    def test_db_init_enforces_body_cache_expiry(self):
        acc = self.db.get_or_create_account("user@example.com")
        old_time = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        self.db.conn.execute(
            "INSERT INTO messages (id, account_id, gmail_message_id, body_preview, fetched_at) VALUES (?, ?, ?, ?, ?)",
            ("old1", acc, "gm-old", "secret old", old_time),
        )
        self.db.conn.commit()
        self.db.close()

        reopened = db_module.DB(self.db_path)
        row = reopened.conn.execute("SELECT body_preview FROM messages WHERE id='old1'").fetchone()
        self.assertIsNone(row["body_preview"])
        self.assertGreaterEqual(reopened.expired_body_count, 1)
        reopened.close()
        self.db = db_module.DB(self.db_path)


class TestSchemaMigration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "EmailMan.db")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_v2_to_v3_adds_account_scan_columns(self):
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
            INSERT INTO schema_version (version) VALUES (2);
            CREATE TABLE accounts (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                gmail_id TEXT UNIQUE,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_sync TIMESTAMP,
                is_active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                thread_id TEXT,
                account_id TEXT NOT NULL,
                gmail_message_id TEXT NOT NULL,
                subject TEXT,
                sender TEXT,
                sender_email TEXT,
                received_at TIMESTAMP,
                body_preview TEXT,
                gmail_labels TEXT,
                fetched_at TIMESTAMP,
                truncated INTEGER NOT NULL DEFAULT 0,
                unsupported_content INTEGER NOT NULL DEFAULT 0,
                has_attachments INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE proposals (
                id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL,
                account_id TEXT NOT NULL,
                label_ids TEXT NOT NULL,
                source TEXT NOT NULL,
                model_version TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                label_definition_version TEXT NOT NULL
            );
            CREATE TABLE decisions (
                id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL,
                account_id TEXT NOT NULL,
                status TEXT NOT NULL,
                label_ids TEXT NOT NULL
            );
            """
        )
        conn.close()

        db_obj = db_module.DB(self.db_path)
        cols = {row["name"] for row in db_obj.conn.execute("PRAGMA table_info(accounts)").fetchall()}
        self.assertIn("last_query", cols)
        self.assertIn("gmail_label_catalog", cols)
        ver = db_obj.conn.execute("SELECT MAX(version) as v FROM schema_version").fetchone()["v"]
        self.assertEqual(ver, db_module.DB.SCHEMA_VERSION)
        db_obj.close()

    def test_add_column_ignores_duplicate_only(self):
        db_obj = db_module.DB(self.db_path)
        cur = db_obj.conn.cursor()
        db_module._add_column_if_missing(cur, "messages", "subject TEXT")
        with self.assertRaises(sqlite3.OperationalError) as cm:
            db_module._add_column_if_missing(cur, "no_such_table", "foo TEXT")
        self.assertNotIn("duplicate column name", str(cm.exception).lower())
        db_obj.close()


class TestMonthWindows(unittest.TestCase):
    def test_three_years_is_36_months_oldest_first(self):
        from datetime import date

        windows = gmail_module.month_windows(3, today=date(2026, 9, 12))
        self.assertEqual(len(windows), 36)
        self.assertEqual(windows[0][0], date(2023, 10, 1))
        self.assertEqual(windows[0][1], date(2023, 11, 1))
        self.assertEqual(windows[-1][0], date(2026, 9, 1))
        self.assertEqual(windows[-1][1], date(2026, 10, 1))
        q = gmail_module.gmail_month_query(windows[0][0], windows[0][1])
        self.assertIn("after:2023/10/01", q)
        self.assertIn("before:2023/11/01", q)
        self.assertIn("-in:trash", q)
        self.assertNotIn("in:inbox", q)


class TestFetchBoundedSample(unittest.TestCase):
    def test_paginates_only_to_cap_and_uses_default_query(self):
        class FakeExecute:
            def __init__(self, payload):
                self._payload = payload

            def execute(self):
                return self._payload

        class FakeGmail:
            def __init__(self):
                self.list_calls = []
                self.get_ids = []

            def users(self):
                return self

            def messages(self):
                return self

            def list(self, userId, q, maxResults, pageToken=None):
                self.list_calls.append({"q": q, "maxResults": maxResults, "pageToken": pageToken})
                page = len(self.list_calls)
                ids = [{"id": f"m{page}-{i}"} for i in range(maxResults)]
                token = "next" if page == 1 else None
                return FakeExecute({"messages": ids, "nextPageToken": token})

            def get(self, userId, id, format):
                self.get_ids.append(id)
                return FakeExecute(
                    {
                        "id": id,
                        "threadId": "t",
                        "labelIds": ["INBOX"],
                        "payload": {
                            "mimeType": "text/plain",
                            "headers": [
                                {"name": "Subject", "value": id},
                                {"name": "From", "value": "a@b.com"},
                            ],
                            "body": {"data": _b64url("body")},
                        },
                    }
                )

        fake = FakeGmail()
        with patch.object(gmail_module.time, "sleep"):
            messages, sample_time = gmail_module.fetch_bounded_sample(fake, limit=25)
        self.assertEqual(len(messages), 25)
        self.assertTrue(sample_time)
        self.assertEqual(fake.list_calls[0]["q"], gmail_module.DEFAULT_GMAIL_QUERY)
        self.assertEqual(fake.list_calls[0]["maxResults"], 20)
        self.assertEqual(fake.list_calls[1]["maxResults"], 5)
        self.assertEqual(len(fake.get_ids), 25)

    def test_on_message_does_not_keep_decoded_list(self):
        seen = []

        class FakeExecute:
            def __init__(self, payload):
                self._payload = payload

            def execute(self):
                return self._payload

        class FakeGmail:
            def users(self):
                return self

            def messages(self):
                return self

            def list(self, userId, q, maxResults, pageToken=None):
                return FakeExecute({"messages": [{"id": "m1"}, {"id": "m2"}]})

            def get(self, userId, id, format):
                return FakeExecute(
                    {
                        "id": id,
                        "threadId": "t",
                        "labelIds": ["INBOX"],
                        "payload": {
                            "mimeType": "text/plain",
                            "headers": [
                                {"name": "Subject", "value": id},
                                {"name": "From", "value": "a@b.com"},
                            ],
                            "body": {"data": _b64url("body")},
                        },
                    }
                )

        with patch.object(gmail_module.time, "sleep"):
            messages, _ = gmail_module.fetch_bounded_sample(
                FakeGmail(), limit=2, on_message=seen.append
            )
        self.assertEqual(len(seen), 2)
        self.assertEqual(messages, [])

    def test_skips_already_cached_ids_without_get(self):
        class FakeExecute:
            def __init__(self, payload):
                self._payload = payload

            def execute(self):
                return self._payload

        class FakeGmail:
            def __init__(self):
                self.get_ids = []

            def users(self):
                return self

            def messages(self):
                return self

            def list(self, userId, q, maxResults, pageToken=None):
                return FakeExecute({"messages": [{"id": "cached"}, {"id": "fresh"}]})

            def get(self, userId, id, format):
                self.get_ids.append(id)
                return FakeExecute(
                    {
                        "id": id,
                        "threadId": "t",
                        "labelIds": ["INBOX"],
                        "payload": {
                            "mimeType": "text/plain",
                            "headers": [
                                {"name": "Subject", "value": id},
                                {"name": "From", "value": "a@b.com"},
                            ],
                            "body": {"data": _b64url("body")},
                        },
                    }
                )

        skipped = []
        fake = FakeGmail()
        with patch.object(gmail_module.time, "sleep"):
            messages, _ = gmail_module.fetch_bounded_sample(
                fake, limit=2, skip_ids={"cached"}, on_skip=skipped.append
            )
        self.assertEqual([m["gmail_message_id"] for m in messages], ["fresh"])
        self.assertEqual(fake.get_ids, ["fresh"])
        self.assertEqual(skipped, ["cached"])

    def test_persistent_get_failures_do_not_walk_the_mailbox(self):
        """Finding 5: failed gets count toward the cap / abort the scan."""
        from EmailMan.gmail import GmailError

        class FakeExecute:
            def __init__(self, payload):
                self._payload = payload

            def execute(self):
                return self._payload

        class FakeGmail:
            def __init__(self):
                self.list_calls = 0

            def users(self):
                return self

            def messages(self):
                return self

            def list(self, userId, q, maxResults, pageToken=None):
                self.list_calls += 1
                ids = [{"id": f"m{self.list_calls}-{i}"} for i in range(maxResults)]
                return FakeExecute({"messages": ids, "nextPageToken": "next"})

            def get(self, userId, id, format):
                raise RuntimeError("boom")

        fake = FakeGmail()
        with patch.object(gmail_module.time, "sleep"):
            with self.assertRaises(GmailError):
                gmail_module.fetch_bounded_sample(fake, limit=100)
        # Without counting failures this would paginate indefinitely.
        self.assertLessEqual(fake.list_calls, 2)

    def test_retries_rate_limit_then_succeeds(self):
        class FakeHttpError(Exception):
            def __init__(self):
                self.resp = type("Resp", (), {"status": 403})()
                super().__init__(
                    "Quota exceeded for quota metric 'Total Query Cost' "
                    "and limit 'Units per minute per user' rateLimitExceeded"
                )

        class FakeExecute:
            def __init__(self, fn):
                self._fn = fn

            def execute(self):
                return self._fn()

        class FakeGmail:
            def __init__(self):
                self.get_tries = 0

            def users(self):
                return self

            def messages(self):
                return self

            def list(self, userId, q, maxResults, pageToken=None):
                return FakeExecute(lambda: {"messages": [{"id": "m1"}]})

            def get(self, userId, id, format):
                def _run():
                    self.get_tries += 1
                    if self.get_tries < 3:
                        raise FakeHttpError()
                    return {
                        "id": id,
                        "threadId": "t",
                        "labelIds": ["INBOX"],
                        "payload": {
                            "mimeType": "text/plain",
                            "headers": [
                                {"name": "Subject", "value": "ok"},
                                {"name": "From", "value": "a@b.com"},
                            ],
                            "body": {"data": _b64url("body")},
                        },
                    }

                return FakeExecute(_run)

        fake = FakeGmail()
        with patch.object(gmail_module.time, "sleep"):
            messages, _ = gmail_module.fetch_bounded_sample(fake, limit=1)
        self.assertEqual(len(messages), 1)
        self.assertEqual(fake.get_tries, 3)

    def test_on_message_runs_before_fetch_returns(self):
        class FakeExecute:
            def __init__(self, payload):
                self._payload = payload

            def execute(self):
                return self._payload

        class FakeGmail:
            def users(self):
                return self

            def messages(self):
                return self

            def list(self, userId, q, maxResults, pageToken=None):
                return FakeExecute({"messages": [{"id": "m1"}]})

            def get(self, userId, id, format):
                return FakeExecute({
                    "id": id,
                    "threadId": "t",
                    "labelIds": ["INBOX"],
                    "payload": {
                        "mimeType": "text/plain",
                        "headers": [
                            {"name": "Subject", "value": "ok"},
                            {"name": "From", "value": "a@b.com"},
                        ],
                        "body": {"data": _b64url("body")},
                    },
                })

        seen = []
        with patch.object(gmail_module.time, "sleep"):
            messages, _ = gmail_module.fetch_bounded_sample(
                FakeGmail(), limit=1, on_message=seen.append
            )
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["gmail_message_id"], "m1")
        self.assertEqual(messages, [])
        self.assertIn("fetched_at", seen[0])


class TestScanAndClearCacheCli(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.temp_dir, "config.json")
        self.config = config_module.Config(self.config_path)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_scan_cmd_stores_query_catalog_and_messages(self):
        decoded = [
            {
                "gmail_message_id": "gm-1",
                "thread_id": "th-1",
                "subject": "Hello",
                "sender": "Alice",
                "sender_email": "alice@example.com",
                "received_at": "2024-01-01T00:00:00+00:00",
                "body_preview": "Body",
                "gmail_labels": ["INBOX"],
                "truncated": False,
                "unsupported_content": False,
                "has_attachments": False,
            }
        ]
        catalog = [{"id": "INBOX", "name": "INBOX", "type": "system"}]
        sample_time = "2024-06-01T12:00:00+00:00"
        args = cli_module.create_cli_parser().parse_args(["--config", self.config_path, "scan"])

        with patch.object(gmail_module, "get_gmail_service", return_value=(object(), "user@example.com")), \
             patch.object(gmail_module, "fetch_account_and_labels", return_value=({"emailAddress": "user@example.com"}, catalog)), \
             patch.object(gmail_module, "fetch_bounded_sample", return_value=(decoded, sample_time)):
            code = cli_module.scan_cmd(args, self.config)
        self.assertEqual(code, 0)

        db_obj = db_module.DB(self.config.database_path)
        acc = db_obj.get_account("user@example.com")
        self.assertEqual(acc["last_query"], gmail_module.DEFAULT_GMAIL_QUERY)
        self.assertEqual(acc["last_sync"], sample_time)
        self.assertEqual(json.loads(acc["gmail_label_catalog"])[0]["id"], "INBOX")
        msg = db_obj.get_message_by_gmail_id("user@example.com", "gm-1")
        self.assertEqual(msg["subject"], "Hello")
        self.assertEqual(msg["fetched_at"], sample_time)
        db_obj.close()

    def test_scan_cmd_classifies_during_fetch(self):
        import threading

        from EmailMan.classification import Proposal

        decoded = [
            {
                "gmail_message_id": "gm-live",
                "thread_id": "th-1",
                "subject": "Hello",
                "sender": "Alice",
                "sender_email": "alice@example.com",
                "received_at": "2024-01-01T00:00:00+00:00",
                "body_preview": "Body",
                "gmail_labels": ["INBOX"],
                "truncated": False,
                "unsupported_content": False,
                "has_attachments": False,
            }
        ]
        catalog = [{"id": "INBOX", "name": "INBOX", "type": "system"}]
        sample_time = "2024-06-01T12:00:00+00:00"
        started = threading.Event()
        args = cli_module.create_cli_parser().parse_args(
            ["--config", self.config_path, "scan", "--classify"]
        )

        def fake_fetch(service, query=None, limit=None, skip_ids=None, on_message=None, on_skip=None):
            if on_message:
                on_message(decoded[0])
            self.assertTrue(started.wait(2), "inference did not start during download")
            return decoded, sample_time

        def fake_classify(self, email_text, label_ids, **kwargs):
            started.set()
            assert "Subject: Hello" in email_text
            assert "From: Alice <alice@example.com>" in email_text
            return Proposal(label_ids=["Type/VerificationCode"], reason="code", abstain=False)

        with patch.object(gmail_module, "get_gmail_service", return_value=(object(), "user@example.com")), \
             patch.object(gmail_module, "fetch_account_and_labels", return_value=({"emailAddress": "user@example.com"}, catalog)), \
             patch.object(gmail_module, "fetch_bounded_sample", side_effect=fake_fetch), \
             patch("EmailMan.classification.TabbyClient.classify_message", fake_classify):
            code = cli_module.scan_cmd(args, self.config)
        self.assertEqual(code, 0)

        db_obj = db_module.DB(self.config.database_path)
        msg = db_obj.get_message_by_gmail_id("user@example.com", "gm-live")
        self.assertIsNotNone(msg)
        self.assertTrue(db_obj.message_has_proposal(msg["id"]))
        db_obj.close()

    def test_scan_classify_worker_death_fails_fast(self):
        """Finding 6: a dead inference worker must not hang the scan."""
        decoded = [
            {
                "gmail_message_id": "gm-dead",
                "thread_id": "th-1",
                "subject": "Hello",
                "sender": "Alice",
                "sender_email": "alice@example.com",
                "received_at": "2024-01-01T00:00:00+00:00",
                "body_preview": "Body",
                "gmail_labels": ["INBOX"],
                "truncated": False,
                "unsupported_content": False,
                "has_attachments": False,
            }
        ]
        catalog = [{"id": "INBOX", "name": "INBOX", "type": "system"}]
        args = cli_module.create_cli_parser().parse_args(
            ["--config", self.config_path, "scan", "--classify"]
        )

        def fake_fetch(service, query=None, limit=None, skip_ids=None, on_message=None, on_skip=None):
            if on_message:
                on_message(decoded[0])
            return decoded, "2024-06-01T12:00:00+00:00"

        def dead_worker(*worker_args, **worker_kwargs):
            raise RuntimeError("worker died")

        with patch.object(gmail_module, "get_gmail_service", return_value=(object(), "user@example.com")), \
             patch.object(gmail_module, "fetch_account_and_labels", return_value=({"emailAddress": "user@example.com"}, catalog)), \
             patch.object(gmail_module, "fetch_bounded_sample", side_effect=fake_fetch), \
             patch("EmailMan.cli._classify_during_scan_worker", side_effect=dead_worker):
            code = cli_module.scan_cmd(args, self.config)
        self.assertNotEqual(code, 0)

    def test_clear_cache_mentions_remaining_sensitive_fields(self):
        db_obj = db_module.DB(self.config.database_path)
        acc = db_obj.get_or_create_account("user@example.com")
        db_obj.upsert_message(
            acc,
            {
                "gmail_message_id": "gm-1",
                "thread_id": "t",
                "subject": "Invoice",
                "sender": "S",
                "sender_email": "s@s",
                "received_at": None,
                "body_preview": "secret",
                "gmail_labels": [],
                "truncated": False,
                "unsupported_content": False,
                "has_attachments": False,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        db_obj.close()
        args = cli_module.create_cli_parser().parse_args(["--config", self.config_path, "clear-cache"])
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            code = cli_module.clear_cache(args, self.config)
        self.assertEqual(code, 0)
        out = buf.getvalue().lower()
        self.assertIn("subject", out)
        self.assertIn("sensitive", out)

    def test_review_start_enforces_expiry_when_db_exists(self):
        db_obj = db_module.DB(self.config.database_path)
        acc = db_obj.get_or_create_account("user@example.com")
        old_time = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        db_obj.conn.execute(
            "INSERT INTO messages (id, account_id, gmail_message_id, body_preview, fetched_at) VALUES (?, ?, ?, ?, ?)",
            ("old1", acc, "gm-old", "secret old", old_time),
        )
        db_obj.conn.commit()
        db_obj.close()

        with patch("flask.Flask.run"):
            app_module.run_review_server(self.config, host="127.0.0.1", port=5000)

        reopened = db_module.DB(self.config.database_path)
        row = reopened.conn.execute("SELECT body_preview FROM messages WHERE id='old1'").fetchone()
        self.assertIsNone(row["body_preview"])
        reopened.close()


class TestGmailWriteHelpers(unittest.TestCase):
    """The label write surface, with a mocked Gmail service."""

    def _service(self):
        return MagicMock()

    def test_list_user_labels_maps_names(self):
        service = self._service()
        service.users.return_value.labels.return_value.list.return_value.execute.return_value = {
            "labels": [
                {"id": "Label_1", "name": "Type/Receipt"},
                {"id": "Label_2", "name": "Newsletter"},
            ]
        }
        self.assertEqual(
            gmail_module.list_user_labels(service),
            {"Type/Receipt": "Label_1", "Newsletter": "Label_2"},
        )

    def test_ensure_label_reuses_existing(self):
        service = self._service()
        self.assertEqual(
            gmail_module.ensure_label(service, "Type/Receipt", {"Type/Receipt": "Label_1"}),
            "Label_1",
        )
        service.users.return_value.labels.return_value.create.assert_not_called()

    def test_ensure_label_creates_when_missing(self):
        service = self._service()
        service.users.return_value.labels.return_value.create.return_value.execute.return_value = {
            "id": "Label_9"
        }
        self.assertEqual(gmail_module.ensure_label(service, "Type/Newsletter", {}), "Label_9")
        body = service.users.return_value.labels.return_value.create.call_args.kwargs["body"]
        self.assertEqual(body["name"], "Type/Newsletter")

    def test_modify_message_labels_add_and_remove(self):
        service = self._service()
        gmail_module.modify_message_labels(
            service, "gm-1", add_label_ids=["A"], remove_label_ids=["B"]
        )
        kwargs = service.users.return_value.messages.return_value.modify.call_args.kwargs
        self.assertEqual(kwargs["id"], "gm-1")
        self.assertEqual(kwargs["body"], {"addLabelIds": ["A"], "removeLabelIds": ["B"]})

    def test_modify_message_labels_is_noop_without_changes(self):
        service = self._service()
        self.assertIsNone(gmail_module.modify_message_labels(service, "gm-1"))
        service.users.return_value.messages.return_value.modify.assert_not_called()

    def test_fetch_message_label_ids_uses_minimal_format(self):
        service = self._service()
        service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
            "labelIds": ["INBOX", "Label_1"]
        }
        self.assertEqual(
            gmail_module.fetch_message_label_ids(service, "gm-1"), ["INBOX", "Label_1"]
        )
        kwargs = service.users.return_value.messages.return_value.get.call_args.kwargs
        self.assertEqual(kwargs["format"], "minimal")


class TestAppliedLabelsStore(unittest.TestCase):
    """The applied-labels snapshot used by sync-labels."""

    def setUp(self):
        self.temp = tempfile.mkdtemp()
        self.db = db_module.DB(os.path.join(self.temp, "EmailMan.db"))
        self.acc = self.db.get_or_create_account("u@example.com")
        self.mid = self.db.upsert_message(
            self.acc,
            {
                "gmail_message_id": "gm-1",
                "thread_id": "th-1",
                "subject": "Receipt",
                "sender": "S",
                "sender_email": "s@e.com",
                "received_at": None,
                "body_preview": "x",
                "gmail_labels": [],
                "truncated": False,
                "unsupported_content": False,
                "has_attachments": False,
            },
        )

    def tearDown(self):
        self.db.close()
        import shutil

        shutil.rmtree(self.temp, ignore_errors=True)

    def test_record_get_and_list(self):
        self.assertIsNone(self.db.get_applied_labels(self.mid))
        self.db.record_applied_labels(self.acc, self.mid, ["Type/Receipt"])
        self.assertEqual(self.db.get_applied_labels(self.mid), ["Type/Receipt"])

        # Upsert replaces the snapshot rather than duplicating it.
        self.db.record_applied_labels(self.acc, self.mid, ["Type/Receipt", "Retention/Forever"])
        self.assertEqual(
            self.db.get_applied_labels(self.mid), ["Type/Receipt", "Retention/Forever"]
        )
        rows = self.db.list_applied_labels()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["gmail_message_id"], "gm-1")


class TestApplyAndSyncCommands(unittest.TestCase):
    """apply-labels / sync-labels against a mocked Gmail service."""

    def setUp(self):
        self.temp = tempfile.mkdtemp()
        self.config_path = os.path.join(self.temp, "config.json")
        self.config = config_module.Config(self.config_path)
        db = db_module.DB(self.config.database_path)
        self.acc = db.get_or_create_account("u@example.com")
        self.mid = db.upsert_message(
            self.acc,
            {
                "gmail_message_id": "gm-1",
                "thread_id": "th-1",
                "subject": "Receipt",
                "sender": "S",
                "sender_email": "s@e.com",
                "received_at": None,
                "body_preview": "x",
                "gmail_labels": [],
                "truncated": False,
                "unsupported_content": False,
                "has_attachments": False,
            },
        )
        db.insert_proposal(self.acc, self.mid, ["Type/Receipt"], "r", "model", "m", "p", "l")
        db.save_decision(self.acc, self.mid, "accepted", ["Type/Receipt"])
        db.close()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.temp, ignore_errors=True)

    def test_apply_dry_run_writes_plan_and_no_gmail(self):
        args = cli_module.create_cli_parser().parse_args(
            ["--config", self.config_path, "apply-labels"]
        )
        with patch.object(gmail_module, "modify_message_labels") as mock_mod:
            code = cli_module.apply_labels_cmd(args, self.config)
        self.assertEqual(code, 0)
        mock_mod.assert_not_called()
        self.assertTrue(os.path.exists(os.path.join(self.temp, "apply-labels-plan.csv")))

    def test_apply_normalizes_legacy_label_ids(self):
        """A decision saved under a retired id is planned as its current label."""
        db = db_module.DB(self.config.database_path)
        db.save_decision(self.acc, self.mid, "corrected", ["Type/Receipt", "Retention/Keep"])
        targets = db.list_gmail_apply_targets()
        db.close()
        self.assertEqual(targets[0]["label_ids"], ["Type/Receipt", "Retention/Forever"])

        args = cli_module.create_cli_parser().parse_args(
            ["--config", self.config_path, "apply-labels"]
        )
        code = cli_module.apply_labels_cmd(args, self.config)
        self.assertEqual(code, 0)
        with open(os.path.join(self.temp, "apply-labels-plan.csv"), encoding="utf-8") as f:
            plan = f.read()
        self.assertIn("Forever", plan)
        self.assertNotIn("Keep", plan)

    def test_apply_creates_labels_and_records_snapshot(self):
        args = cli_module.create_cli_parser().parse_args(
            ["--config", self.config_path, "apply-labels", "--apply"]
        )
        with patch.object(
            gmail_module, "get_gmail_service", return_value=(MagicMock(), "u@example.com")
        ), patch.object(
            gmail_module, "get_stored_scopes", return_value=[gmail_module.GMAIL_MODIFY_SCOPE]
        ), patch.object(
            gmail_module, "list_user_labels", return_value={"Type/Receipt": "Label_1"}
        ), patch.object(
            gmail_module, "ensure_label", return_value="Label_1"
        ), patch.object(
            gmail_module, "modify_message_labels"
        ) as mock_mod:
            code = cli_module.apply_labels_cmd(args, self.config)
        self.assertEqual(code, 0)
        self.assertEqual(mock_mod.call_args.kwargs["add_label_ids"], ["Label_1"])

        db = db_module.DB(self.config.database_path)
        try:
            self.assertEqual(db.get_applied_labels(self.mid), ["Type/Receipt"])
        finally:
            db.close()

    def test_apply_refuses_without_modify_scope(self):
        args = cli_module.create_cli_parser().parse_args(
            ["--config", self.config_path, "apply-labels", "--apply"]
        )
        with patch.object(
            gmail_module, "get_gmail_service", return_value=(MagicMock(), "u@example.com")
        ), patch.object(
            gmail_module, "get_stored_scopes", return_value=[gmail_module.GMAIL_READONLY_SCOPE]
        ):
            code = cli_module.apply_labels_cmd(args, self.config)
        self.assertEqual(code, 1)

    def test_sync_records_gmail_edits_as_corrections(self):
        db = db_module.DB(self.config.database_path)
        db.record_applied_labels(self.acc, self.mid, ["Type/Receipt"])
        db.close()

        args = cli_module.create_cli_parser().parse_args(
            ["--config", self.config_path, "sync-labels"]
        )
        with patch.object(
            gmail_module, "get_gmail_service", return_value=(MagicMock(), "u@example.com")
        ), patch.object(
            gmail_module,
            "list_user_labels",
            return_value={"Type/Receipt": "Label_1", "Type/Newsletter": "Label_2"},
        ), patch.object(
            gmail_module, "fetch_message_label_ids", return_value=["Label_2"]
        ):
            code = cli_module.sync_labels_cmd(args, self.config)
        self.assertEqual(code, 0)

        db = db_module.DB(self.config.database_path)
        try:
            decision = db.get_decision(self.mid)
            self.assertEqual(decision["status"], "corrected")
            self.assertEqual(decision["label_ids_parsed"], ["Type/Newsletter"])
            self.assertEqual(db.get_applied_labels(self.mid), ["Type/Newsletter"])
        finally:
            db.close()

    def test_sync_leaves_untouched_message_accepted(self):
        db = db_module.DB(self.config.database_path)
        db.record_applied_labels(self.acc, self.mid, ["Type/Receipt"])
        db.close()

        args = cli_module.create_cli_parser().parse_args(
            ["--config", self.config_path, "sync-labels"]
        )
        with patch.object(
            gmail_module, "get_gmail_service", return_value=(MagicMock(), "u@example.com")
        ), patch.object(
            gmail_module, "list_user_labels", return_value={"Type/Receipt": "Label_1"}
        ), patch.object(
            gmail_module, "fetch_message_label_ids", return_value=["INBOX", "Label_1"]
        ):
            code = cli_module.sync_labels_cmd(args, self.config)
        self.assertEqual(code, 0)

        db = db_module.DB(self.config.database_path)
        try:
            self.assertEqual(db.get_decision(self.mid)["status"], "accepted")
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
