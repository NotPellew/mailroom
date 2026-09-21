"""Tests for local email ingestion, EML parsing, schema v6 migration, and decoupled review."""

import argparse
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from EmailMan import db, export, ingest
from EmailMan.cli import classify_cmd, ingest_cmd


SAMPLE_PLAIN_EML = b"""From: Alice Example <alice@example.com>
To: Bob Receiver <bob@example.com>
Subject: Project Status Update
Date: Mon, 21 Sep 2026 10:30:00 +0000
Message-ID: <unique-msg-12345@example.com>
Content-Type: text/plain; charset="utf-8"

Hi Bob,
Here is the weekly update on the project.
Best,
Alice
"""

SAMPLE_HTML_EML = b"""From: billing@service.com
To: user@example.com
Subject: Your Receipt
Date: Mon, 21 Sep 2026 11:00:00 +0000
Content-Type: text/html; charset="utf-8"

<html>
<head><style>body { font-family: sans-serif; }</style></head>
<body>
<script>alert('malicious')</script>
<p>Thank you for your purchase.</p>
<p>Total: $42.00</p>
</body>
</html>
"""

SAMPLE_MULTIPART_WITH_ATTACHMENT = b"""From: hr@company.com
To: employee@company.com
Subject: Contract
Date: Mon, 21 Sep 2026 12:00:00 +0000
Message-ID: <contract-999@company.com>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="BOUNDARY123"

--BOUNDARY123
Content-Type: text/plain; charset="utf-8"

Please find the attached agreement.

--BOUNDARY123
Content-Type: application/pdf
Content-Disposition: attachment; filename="agreement.pdf"
Content-Transfer-Encoding: base64

JVBERi0xLjQK
--BOUNDARY123--
"""

NESTED_ATTACHMENT_EML = b"""From: outer@example.com
To: user@example.com
Subject: Outer Message
Date: Mon, 21 Sep 2026 13:00:00 +0000
Message-ID: <outer-1@example.com>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="OUTERBOUND"

--OUTERBOUND
Content-Type: text/html; charset="utf-8"

<html><body><p>Outer HTML body text.</p></body></html>

--OUTERBOUND
Content-Type: message/rfc822
Content-Disposition: attachment; filename="inner.eml"

From: inner@example.com
Subject: Inner Message
Content-Type: text/plain; charset="utf-8"

INNER PLAIN BODY SHOULD NOT BE USED
--OUTERBOUND--
"""


class TestEmlParsing(unittest.TestCase):
    """Test RFC 822 / MIME parsing in EmailMan.ingest."""

    def test_parse_eml_plain_text(self):
        decoded = ingest.parse_eml_bytes(SAMPLE_PLAIN_EML, source="local")
        self.assertEqual(decoded["subject"], "Project Status Update")
        self.assertEqual(decoded["sender"], "Alice Example")
        self.assertEqual(decoded["sender_email"], "alice@example.com")
        self.assertEqual(decoded["message_id"], "unique-msg-12345@example.com")
        self.assertIn("weekly update", decoded["body_preview"])
        self.assertEqual(decoded["source"], "local")
        self.assertIsNone(decoded["gmail_message_id"])
        self.assertFalse(decoded["has_attachments"])
        self.assertFalse(decoded["truncated"])
        self.assertFalse(decoded["unsupported_content"])
        self.assertIsNotNone(decoded["received_at"])

    def test_parse_eml_missing_message_id_uses_sha256(self):
        # SAMPLE_HTML_EML has no Message-ID header
        decoded1 = ingest.parse_eml_bytes(SAMPLE_HTML_EML, source="local")
        decoded2 = ingest.parse_eml_bytes(SAMPLE_HTML_EML, source="local")

        self.assertTrue(decoded1["message_id"].startswith("eml_"))
        self.assertEqual(len(decoded1["message_id"]), 28)  # 'eml_' (4) + 24 hex
        # Deterministic: identical bytes produce identical message_id
        self.assertEqual(decoded1["message_id"], decoded2["message_id"])

    def test_parse_eml_html_only_strips_scripts_and_styles(self):
        decoded = ingest.parse_eml_bytes(SAMPLE_HTML_EML, source="local")
        self.assertEqual(decoded["subject"], "Your Receipt")
        self.assertEqual(decoded["sender_email"], "billing@service.com")
        self.assertIn("Thank you for your purchase.", decoded["body_preview"])
        self.assertIn("Total: $42.00", decoded["body_preview"])
        self.assertNotIn("alert", decoded["body_preview"])
        self.assertNotIn("<script>", decoded["body_preview"])
        self.assertNotIn("<style>", decoded["body_preview"])

    def test_parse_eml_detects_attachments(self):
        decoded = ingest.parse_eml_bytes(SAMPLE_MULTIPART_WITH_ATTACHMENT, source="local")
        self.assertTrue(decoded["has_attachments"])
        self.assertIn("Please find the attached agreement.", decoded["body_preview"])

    def test_parse_eml_truncation(self):
        huge_body = "A" * 5000
        raw = f"Subject: Huge\nFrom: x@y.com\n\n{huge_body}".encode("utf-8")
        decoded = ingest.parse_eml_bytes(raw, source="local")
        self.assertTrue(decoded["truncated"])
        self.assertEqual(len(decoded["body_preview"]), 4000)

    def test_parse_eml_nested_attachment_keeps_outer_body(self):
        decoded = ingest.parse_eml_bytes(NESTED_ATTACHMENT_EML, source="local")
        self.assertIn("Outer HTML body text.", decoded["body_preview"])
        self.assertNotIn("INNER PLAIN BODY", decoded["body_preview"])
        self.assertTrue(decoded["has_attachments"])

    def test_parse_eml_empty_body_unsupported_content(self):
        empty_eml = b"From: sender@example.com\nSubject: Empty\n\n"
        decoded = ingest.parse_eml_bytes(empty_eml, source="local")
        self.assertEqual(decoded["body_preview"], "")
        self.assertTrue(decoded["unsupported_content"])


class TestLocalIngestion(unittest.TestCase):
    """Test database ingestion of local emails."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        self.database = db.DB(str(self.db_path))

    def tearDown(self):
        self.database.close()
        self.tmpdir.cleanup()

    def test_ingest_single_file(self):
        eml_file = Path(self.tmpdir.name) / "test.eml"
        eml_file.write_bytes(SAMPLE_PLAIN_EML)

        result = ingest.ingest_path(self.database, eml_file)
        self.assertEqual(result.total_found, 1)
        self.assertEqual(result.ingested, 1)
        self.assertEqual(result.failed, 0)
        self.assertEqual(len(result.message_ids), 1)

        msg_id = result.message_ids[0]
        row = self.database.get_message(msg_id)
        self.assertIsNotNone(row)
        self.assertEqual(row["subject"], "Project Status Update")
        self.assertEqual(row["source"], "local")
        self.assertIsNone(row["gmail_message_id"])

    def test_ingest_missing_path_raises_filenotfound(self):
        nonexistent = Path(self.tmpdir.name) / "does_not_exist.eml"
        with self.assertRaises(FileNotFoundError):
            ingest.ingest_path(self.database, nonexistent)

    def test_ingest_corrupt_file_increments_failed(self):
        corrupt_file = Path(self.tmpdir.name) / "corrupt.eml"
        corrupt_file.write_bytes(SAMPLE_PLAIN_EML)
        with patch.object(Path, "read_bytes", side_effect=OSError("Read error")):
            result = ingest.ingest_path(self.database, corrupt_file)
            self.assertEqual(result.total_found, 1)
            self.assertEqual(result.ingested, 0)
            self.assertEqual(result.failed, 1)

    def test_ingest_directory_recursive_and_non_recursive(self):
        sub_dir = Path(self.tmpdir.name) / "subdir"
        sub_dir.mkdir()

        f1 = Path(self.tmpdir.name) / "root.eml"
        f2 = sub_dir / "nested.eml"
        f3 = sub_dir / "other.txt"  # Not an .eml file

        f1.write_bytes(SAMPLE_PLAIN_EML)
        f2.write_bytes(SAMPLE_HTML_EML)
        f3.write_text("Hello text")

        # Recursive (default)
        res_recursive = ingest.ingest_path(self.database, self.tmpdir.name, recursive=True)
        self.assertEqual(res_recursive.total_found, 2)
        self.assertEqual(res_recursive.ingested, 2)

        # Non-recursive
        database2_path = Path(self.tmpdir.name) / "test2.db"
        database2 = db.DB(str(database2_path))
        try:
            res_flat = ingest.ingest_path(database2, self.tmpdir.name, recursive=False)
            self.assertEqual(res_flat.total_found, 1)
            self.assertEqual(res_flat.ingested, 1)
        finally:
            database2.close()

    def test_ingest_limit(self):
        f1 = Path(self.tmpdir.name) / "msg1.eml"
        f2 = Path(self.tmpdir.name) / "msg2.eml"
        f1.write_bytes(SAMPLE_PLAIN_EML)
        f2.write_bytes(SAMPLE_HTML_EML)

        result = ingest.ingest_path(self.database, self.tmpdir.name, limit=1)
        self.assertEqual(result.total_found, 2)
        self.assertEqual(result.ingested, 1)

    def test_ingest_idempotency_preserves_decisions(self):
        eml_file = Path(self.tmpdir.name) / "test.eml"
        eml_file.write_bytes(SAMPLE_PLAIN_EML)

        res1 = ingest.ingest_path(self.database, eml_file)
        msg_id = res1.message_ids[0]

        # Record human decision
        self.database.save_decision(
            account_id="local",
            message_id=msg_id,
            status="accepted",
            label_ids=["Type/NeedsReply"],
        )

        # Re-ingest
        res2 = ingest.ingest_path(self.database, eml_file)
        self.assertEqual(res2.ingested, 1)
        self.assertEqual(res2.message_ids[0], msg_id)

        # Confirm decision is intact
        review_item = self.database.get_review_item(msg_id)
        self.assertEqual(review_item["review_status"], "accepted")
        self.assertEqual(review_item["decision_label_ids"], ["Type/NeedsReply"])

    def test_source_filtering_in_review_items(self):
        # Insert one local message and one Gmail message
        eml_file = Path(self.tmpdir.name) / "local.eml"
        eml_file.write_bytes(SAMPLE_PLAIN_EML)
        ingest.ingest_path(self.database, eml_file)

        # Create Gmail account and message
        acc_id = self.database.get_or_create_account("user@gmail.com")
        self.database.upsert_message(
            acc_id,
            {
                "gmail_message_id": "gm-123",
                "source": "gmail",
                "subject": "Gmail Subject",
                "body_preview": "Gmail body",
            },
        )

        all_items = self.database.list_review_items()
        self.assertEqual(len(all_items), 2)

        local_items = self.database.list_review_items(source="local")
        self.assertEqual(len(local_items), 1)
        self.assertEqual(local_items[0]["source"], "local")

        gmail_items = self.database.list_review_items(source="gmail")
        self.assertEqual(len(gmail_items), 1)
        self.assertEqual(gmail_items[0]["source"], "gmail")

    def test_export_includes_local_messages(self):
        eml_file = Path(self.tmpdir.name) / "test.eml"
        eml_file.write_bytes(SAMPLE_PLAIN_EML)
        res = ingest.ingest_path(self.database, eml_file)
        msg_id = res.message_ids[0]

        self.database.save_decision(
            account_id="local",
            message_id=msg_id,
            status="accepted",
            label_ids=["Type/Receipt"],
        )

        decisions = self.database.list_decisions_for_export()
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["source"], "local")
        self.assertEqual(decisions[0]["message_id"], msg_id)

        records = export.build_export_records(decisions)
        self.assertEqual(len(records), 1)
        # For non-gmail message, message_id in export record is the local id
        self.assertEqual(records[0]["message_id"], msg_id)


class TestSchemaV5ToV6Migration(unittest.TestCase):
    """Test schema v5 to v6 upgrade with nullability relaxation."""

    def test_migration_relaxes_gmail_message_id_not_null(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_file = Path(tmpdir) / "v5.db"
            conn = sqlite3.connect(str(db_file))
            # Create schema v5 table with NOT NULL constraint on gmail_message_id
            conn.executescript(
                """
                CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
                INSERT INTO schema_version (version) VALUES (5);
                CREATE TABLE accounts (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    email TEXT NOT NULL,
                    gmail_id TEXT UNIQUE,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_sync TIMESTAMP,
                    last_query TEXT,
                    gmail_label_catalog TEXT,
                    is_active INTEGER NOT NULL DEFAULT 1
                );
                INSERT INTO accounts (id, name, email, gmail_id)
                VALUES ('user@example.com', 'User', 'user@example.com', 'user@example.com');
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
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE,
                    UNIQUE (account_id, gmail_message_id)
                );
                INSERT INTO messages (id, thread_id, account_id, gmail_message_id, subject, body_preview)
                VALUES ('user@example.com:gm1', 't1', 'user@example.com', 'gm1', 'Old Gmail Msg', 'Body text');
                CREATE TABLE proposals (
                    id TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    label_ids TEXT NOT NULL,
                    label_names TEXT,
                    reason TEXT,
                    confidence REAL,
                    abstain INTEGER NOT NULL DEFAULT 0,
                    source TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    label_definition_version TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE,
                    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
                );
                CREATE TABLE decisions (
                    id TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    label_ids TEXT NOT NULL,
                    proposal_id TEXT,
                    user_notes TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE,
                    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE,
                    UNIQUE (account_id, message_id)
                );
                INSERT INTO decisions (id, message_id, account_id, status, label_ids)
                VALUES ('d1', 'user@example.com:gm1', 'user@example.com', 'accepted', '["Type/Receipt"]');
                """
            )
            conn.close()

            # Open with DB class to apply v5 -> v6 migration
            db_obj = db.DB(str(db_file))
            try:
                # Schema version must be 6
                ver = db_obj.conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
                self.assertEqual(ver, 6)

                # Foreign keys must remain enabled post-migration
                fk_on = db_obj.conn.execute("PRAGMA foreign_keys").fetchone()[0]
                self.assertEqual(fk_on, 1)
                fk_check = db_obj.conn.execute("PRAGMA foreign_key_check").fetchall()
                self.assertEqual(fk_check, [])

                # Prior message and decision preserved
                old_msg = db_obj.get_message("user@example.com:gm1")
                self.assertIsNotNone(old_msg)
                self.assertEqual(old_msg["subject"], "Old Gmail Msg")
                self.assertEqual(old_msg["source"], "gmail")

                old_review = db_obj.get_review_item("user@example.com:gm1")
                self.assertEqual(old_review["review_status"], "accepted")

                # Insert a non-Gmail message with NULL gmail_message_id
                local_id = db_obj.upsert_message(
                    "user@example.com",
                    {
                        "message_id": "local_test_123",
                        "source": "local",
                        "subject": "Local Message",
                        "body_preview": "Local body",
                    },
                )
                self.assertEqual(local_id, "user@example.com:local:local_test_123")
                local_row = db_obj.get_message(local_id)
                self.assertIsNotNone(local_row)
                self.assertIsNone(local_row["gmail_message_id"])
                self.assertEqual(local_row["source"], "local")
            finally:
                db_obj.close()


class TestCliIngestAndClassify(unittest.TestCase):
    """Test CLI commands for ingest and classifying local messages."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.tmpdir.name) / "config.json"
        cfg_dict = {
            "model": {
                "endpoint": "http://127.0.0.1:11434",
                "id": "qwen2.5:7b",
                "provider": "ollama",
            },
            "timeout": 30.0,
            "labels": [
                {"id": "Type/Receipt", "name": "Receipt", "axis": "kind", "description": "Receipt"},
                {"id": "Type/Newsletter", "name": "Newsletter", "axis": "kind", "description": "News"},
            ],
        }
        self.config_path.write_text(json.dumps(cfg_dict), encoding="utf-8")
        from EmailMan.config import Config

        self.config = Config(str(self.config_path))
        self.db_path = Path(self.config.database_path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_cli_ingest_command(self):
        eml_file = Path(self.tmpdir.name) / "test.eml"
        eml_file.write_bytes(SAMPLE_PLAIN_EML)

        args = argparse.Namespace(
            path=str(eml_file),
            account="test_local",
            limit=None,
            recursive=True,
        )

        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            code = ingest_cmd(args, self.config)

        self.assertEqual(code, 0)
        self.assertIn("Ingested 1/1 message(s) into account 'test_local'", stdout.getvalue())

        database = db.DB(str(self.db_path))
        try:
            items = database.list_review_items()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["account_id"], "test_local")
            self.assertEqual(items[0]["source"], "local")
        finally:
            database.close()

    def test_classify_local_message_by_id(self):
        eml_file = Path(self.tmpdir.name) / "test.eml"
        eml_file.write_bytes(SAMPLE_PLAIN_EML)
        ingest_args = argparse.Namespace(
            path=str(eml_file),
            account="local",
            limit=None,
            recursive=True,
        )
        ingest_cmd(ingest_args, self.config)

        database = db.DB(str(self.db_path))
        items = database.list_review_items()
        msg_id = items[0]["message_id"]
        database.close()

        classify_args = argparse.Namespace(
            message_id=msg_id,
            limit=None,
            model_endpoint=None,
            reclassify=False,
            offset=0,
        )

        from EmailMan.classification import Proposal

        fake_proposal = Proposal(
            label_ids=["Type/Receipt"],
            confidence=0.95,
            reason="Sample receipt",
            abstain=False,
        )

        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            with patch("EmailMan.classification.classify_message", return_value=fake_proposal):
                code = classify_cmd(classify_args, self.config)

        self.assertEqual(code, 0)
        self.assertIn("Labels: Type/Receipt", stdout.getvalue())

        database = db.DB(str(self.db_path))
        try:
            review = database.get_review_item(msg_id)
            self.assertEqual(review["proposal_label_ids"], ["Type/Receipt"])
        finally:
            database.close()

    def test_batch_classify_local_messages(self):
        eml1 = Path(self.tmpdir.name) / "msg1.eml"
        eml2 = Path(self.tmpdir.name) / "msg2.eml"
        eml1.write_bytes(SAMPLE_PLAIN_EML)
        eml2.write_bytes(SAMPLE_HTML_EML)

        ingest_args = argparse.Namespace(
            path=str(self.tmpdir.name),
            account="batch_local",
            limit=None,
            recursive=False,
        )
        self.assertEqual(ingest_cmd(ingest_args, self.config), 0)

        classify_args = argparse.Namespace(
            message_id=None,
            limit=5,
            model_endpoint=None,
            reclassify=False,
            offset=0,
        )

        from EmailMan.classification import Proposal

        fake_proposal = Proposal(
            label_ids=["Type/Newsletter"],
            confidence=0.88,
            reason="Batch test",
            abstain=False,
        )

        with patch("sys.stdout", io.StringIO()):
            with patch("EmailMan.classification.classify_message", return_value=fake_proposal):
                code = classify_cmd(classify_args, self.config)

        self.assertEqual(code, 0)
        database = db.DB(str(self.db_path))
        try:
            items = database.list_review_items(source="local")
            self.assertEqual(len(items), 2)
            for item in items:
                self.assertEqual(item["proposal_label_ids"], ["Type/Newsletter"])
        finally:
            database.close()


class TestReviewRoutesForLocalMessage(unittest.TestCase):
    """Test Flask routes rendering of local messages."""

    def test_review_payload_local_message_has_no_gmail_url(self):
        from EmailMan.routes import _review_item_payload

        local_item = {
            "message_id": "local:12345",
            "source": "local",
            "gmail_message_id": None,
            "subject": "Local Subject",
            "account_email": "local@localhost",
            "has_body": True,
            "body_preview": "Sample body",
        }
        payload = _review_item_payload(local_item, include_body=True)
        self.assertEqual(payload["source"], "local")
        self.assertIsNone(payload["gmail_url"])
        self.assertEqual(payload["message_id"], "local:12345")

    def test_api_classify_route_with_local_message(self):
        from EmailMan.classification import Proposal

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            cfg_dict = {
                "model": {"endpoint": "http://127.0.0.1:11434", "id": "test-model", "provider": "ollama"},
                "timeout": 30.0,
                "labels": [{"id": "Type/Receipt", "name": "Receipt", "axis": "kind"}],
            }
            config_path.write_text(json.dumps(cfg_dict), encoding="utf-8")
            from EmailMan.config import Config

            cfg = Config(str(config_path))

            database = db.DB(str(cfg.database_path))
            database.get_or_create_account(email="local", name="Local Mailbox", is_local=True)
            msg_id = database.upsert_message(
                "local",
                {
                    "message_id": "test_local_msg_1",
                    "source": "local",
                    "subject": "Local Bill",
                    "sender": "Store",
                    "sender_email": "store@example.com",
                    "body_preview": "Your total is $10",
                },
            )
            database.close()

            from EmailMan.app import create_app

            app = create_app(cfg)
            client = app.test_client()
            csrf = app.config["CSRF_TOKEN"]

            fake_proposal = Proposal(
                label_ids=["Type/Receipt"],
                confidence=0.9,
                reason="Receipt found",
                abstain=False,
            )

            with patch("EmailMan.classification.classify_message", return_value=fake_proposal):
                resp = client.post(
                    "/api/classify",
                    json={"message_id": msg_id},
                    headers={"X-CSRF-Token": csrf},
                )
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertEqual(data["label_ids"], ["Type/Receipt"])


class TestLocalIdentityIsolation(unittest.TestCase):
    """Local ids are namespaced and cannot collide with Gmail identity."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        self.database = db.DB(str(self.db_path))

    def tearDown(self):
        self.database.close()
        self.tmpdir.cleanup()

    def test_local_message_id_cannot_collide_with_gmail_row(self):
        acc = self.database.get_or_create_account("user@gmail.com")
        gmail_id = self.database.upsert_message(
            acc,
            {
                "gmail_message_id": "gm1",
                "source": "gmail",
                "subject": "Gmail Subject",
                "body_preview": "Gmail body",
            },
        )
        self.database.save_decision(acc, gmail_id, "accepted", label_ids=["Type/Receipt"])

        eml_file = Path(self.tmpdir.name) / "collide.eml"
        eml_file.write_bytes(
            b"From: local@example.com\n"
            b"Subject: Local Imposter\n"
            b"Message-ID: <gm1>\n"
            b'Content-Type: text/plain; charset="utf-8"\n\n'
            b"Local body text\n"
        )
        result = ingest.ingest_path(self.database, eml_file, account_id="user@gmail.com")
        self.assertEqual(result.ingested, 1)

        local_id = result.message_ids[0]
        self.assertEqual(local_id, f"{acc}:local:gm1")
        self.assertNotEqual(local_id, gmail_id)

        count = self.database.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE account_id = ?", (acc,)
        ).fetchone()[0]
        self.assertEqual(count, 2)

        gmail_row = self.database.get_message(gmail_id)
        self.assertEqual(gmail_row["subject"], "Gmail Subject")
        self.assertEqual(gmail_row["body_preview"], "Gmail body")
        self.assertEqual(gmail_row["source"], "gmail")
        self.assertEqual(
            self.database.get_review_item(gmail_id)["review_status"], "accepted"
        )

        targets = self.database.list_gmail_apply_targets()
        self.assertEqual([t["gmail_message_id"] for t in targets], ["gm1"])

    def test_local_ids_do_not_collapse_with_alias_prefix(self):
        self.database.get_or_create_account("local", is_local=True)
        first = self.database.upsert_message(
            "local", {"message_id": "foo", "source": "local", "body_preview": "a"}
        )
        second = self.database.upsert_message(
            "local", {"message_id": "local:foo", "source": "local", "body_preview": "b"}
        )

        self.assertEqual(first, "local:local:foo")
        self.assertEqual(second, "local:local:local:foo")
        self.assertNotEqual(first, second)
        count = self.database.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE account_id = 'local'"
        ).fetchone()[0]
        self.assertEqual(count, 2)

    def test_ingest_uses_canonical_account_id(self):
        self.database.get_or_create_account("user@gmail.com")
        eml_file = Path(self.tmpdir.name) / "canonical.eml"
        eml_file.write_bytes(SAMPLE_PLAIN_EML)

        result = ingest.ingest_path(self.database, eml_file, account_id="User@gmail.com")

        self.assertEqual(result.ingested, 1)
        self.assertEqual(result.failed, 0)
        self.assertEqual(result.account_id, "user@gmail.com")
        row = self.database.get_message(result.message_ids[0])
        self.assertEqual(row["account_id"], "user@gmail.com")


class TestGmailAccountPromotion(unittest.TestCase):
    """Gmail auth promotes a local row instead of leaving the mix guard open."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.database = db.DB(str(Path(self.tmpdir.name) / "test.db"))

    def tearDown(self):
        self.database.close()
        self.tmpdir.cleanup()

    def test_gmail_auth_promotes_and_blocks_second_account(self):
        self.database.get_or_create_account("alice@gmail.com", is_local=True)
        self.assertIsNone(self.database.get_account("alice@gmail.com")["gmail_id"])

        acc = self.database.get_or_create_account("alice@gmail.com")

        self.assertEqual(acc, "alice@gmail.com")
        self.assertEqual(
            self.database.get_account("alice@gmail.com")["gmail_id"], "alice@gmail.com"
        )
        with self.assertRaises(db.DatabaseError):
            self.database.get_or_create_account("bob@gmail.com")

    def test_gmail_promote_refused_when_another_mailbox_active(self):
        self.database.get_or_create_account("bob@gmail.com")
        self.database.get_or_create_account("alice@gmail.com", is_local=True)

        with self.assertRaises(db.DatabaseError):
            self.database.get_or_create_account("alice@gmail.com")

        self.assertEqual(
            self.database.get_account("bob@gmail.com")["gmail_id"], "bob@gmail.com"
        )
        self.assertIsNone(self.database.get_account("alice@gmail.com")["gmail_id"])


class TestListCachedGmailIds(unittest.TestCase):
    """Only rows with a Gmail id and cached body count as cached Gmail ids."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.database = db.DB(str(Path(self.tmpdir.name) / "test.db"))

    def tearDown(self):
        self.database.close()
        self.tmpdir.cleanup()

    def test_local_rows_are_excluded(self):
        acc = self.database.get_or_create_account("user@gmail.com", is_local=True)
        self.database.upsert_message(
            acc,
            {"message_id": "local-1", "source": "local", "body_preview": "local body"},
        )
        self.database.upsert_message(
            acc,
            {"gmail_message_id": "gm-1", "source": "gmail", "body_preview": "gmail body"},
        )

        cached = self.database.list_cached_gmail_ids(acc)

        self.assertEqual(cached, {"gm-1"})


class TestSourceAwareBodyExpiry(unittest.TestCase):
    """The 7-day Gmail body cache never clears locally ingested bodies."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.database = db.DB(str(Path(self.tmpdir.name) / "test.db"))

    def tearDown(self):
        self.database.close()
        self.tmpdir.cleanup()

    def _age(self, message_id: str) -> None:
        old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        self.database.conn.execute(
            "UPDATE messages SET fetched_at = ? WHERE id = ?", (old, message_id)
        )
        self.database.conn.commit()

    def test_local_body_survives_while_gmail_cache_expires(self):
        self.database.get_or_create_account("local", is_local=True)
        local_id = self.database.upsert_message(
            "local",
            {"message_id": "keep-me", "source": "local", "body_preview": "local body"},
        )
        self._age(local_id)

        acc = self.database.get_or_create_account("gmail-user@gmail.com")
        gmail_id = self.database.upsert_message(
            acc,
            {
                "gmail_message_id": "gm-expire",
                "source": "gmail",
                "body_preview": "gmail body",
            },
        )
        self._age(gmail_id)

        expired = self.database.enforce_body_cache_expiry()

        self.assertEqual(expired, 1)
        self.assertEqual(self.database.get_message(local_id)["body_preview"], "local body")
        self.assertIsNone(self.database.get_message(gmail_id)["body_preview"])


class TestApplyTargetsSourceAware(unittest.TestCase):
    """Only Gmail-backed decisions become Gmail apply targets."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.database = db.DB(str(Path(self.tmpdir.name) / "test.db"))

    def tearDown(self):
        self.database.close()
        self.tmpdir.cleanup()

    def test_local_decisions_are_not_gmail_targets(self):
        self.database.get_or_create_account("local", is_local=True)
        local_id = self.database.upsert_message(
            "local",
            {"message_id": "local-plan", "source": "local", "body_preview": "local"},
        )
        self.database.save_decision("local", local_id, "accepted", label_ids=["Type/Receipt"])

        acc = self.database.get_or_create_account("gmail-user@gmail.com")
        gmail_id = self.database.upsert_message(
            acc,
            {
                "gmail_message_id": "gm-plan",
                "source": "gmail",
                "body_preview": "gmail",
            },
        )
        self.database.save_decision(acc, gmail_id, "accepted", label_ids=["Type/Receipt"])

        targets = self.database.list_gmail_apply_targets()
        self.assertEqual([t["gmail_message_id"] for t in targets], ["gm-plan"])


class TestLocalEmptyBodyNotice(unittest.TestCase):
    """The empty-body notice must not point local mail at Gmail."""

    def test_local_empty_body_notice_does_not_reference_gmail(self):
        from EmailMan.routes import _review_item_payload

        payload = _review_item_payload(
            {
                "message_id": "local:local:gone",
                "source": "local",
                "gmail_message_id": None,
                "subject": "Local",
                "account_email": "local@localhost",
                "has_body": False,
                "body_preview": None,
            },
            include_body=True,
        )
        self.assertIsNone(payload["gmail_url"])

        template_path = (
            Path(__file__).resolve().parent.parent / "EmailMan" / "templates" / "index.html"
        )
        local_branch = next(
            line
            for line in template_path.read_text(encoding="utf-8").splitlines()
            if ': "Body cache expired' in line
        )
        self.assertIn("re-run emailman ingest", local_branch)
        self.assertNotIn("Gmail", local_branch)


if __name__ == "__main__":
    unittest.main()
