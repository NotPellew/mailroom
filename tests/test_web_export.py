"""End-to-end tests for web decision export (CSV/JSON) and status counts."""

import csv
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from Mailroom import app as app_module
from Mailroom.config import Config
from Mailroom.db import DB
from Mailroom.export import EXPORT_FIELDS


class TestWebExport(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmpdir.name)
        self.config_path = str(self.tmp_path / "config.json")
        self.config = Config(self.config_path)
        self.config.save()
        self.db = DB(self.config.database_path)

        self.app = app_module.create_app(self.config)
        self.client = self.app.test_client()
        self.csrf = self.app.config["CSRF_TOKEN"]

    def tearDown(self):
        self.db.close()
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

    def _seed_decisions(self):
        acc_id = self.db.get_or_create_account(email="local", is_local=True)
        m1 = self.db.upsert_message(
            acc_id,
            {
                "message_id": "msg-1",
                "subject": "Invoice for services",
                "sender": "billing@example.com",
                "sender_email": "billing@example.com",
                "body_preview": "Please find attached invoice.",
            },
        )
        m2 = self.db.upsert_message(
            acc_id,
            {
                "message_id": "msg-2",
                "subject": "Weekly Newsletter",
                "sender": "news@example.com",
                "sender_email": "news@example.com",
                "body_preview": "Here are this week's updates.",
            },
        )
        m3 = self.db.upsert_message(
            acc_id,
            {
                "message_id": "msg-3",
                "subject": "Spam or irrelevant message",
                "sender": "promo@example.com",
                "sender_email": "promo@example.com",
                "body_preview": "Special discount for you.",
            },
        )
        self.db.save_decision(acc_id, m1, "accepted", ["Type/Receipt"])
        self.db.save_decision(acc_id, m2, "corrected", ["Type/Newsletter"])
        self.db.save_decision(acc_id, m3, "skipped", [])
        return acc_id, m1, m2, m3

    def test_export_csv_download_headers_and_filename(self):
        self._seed_decisions()
        res = self.client.get("/api/export?format=csv", headers=self.headers())
        self.assertEqual(res.status_code, 200)

        # Content-Type and charset verification
        self.assertEqual(res.mimetype, "text/csv")
        self.assertIn("charset=utf-8", res.headers.get("Content-Type", "").lower())

        # Date-stamped Content-Disposition filename verification
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        expected_disposition = f'attachment; filename="mailroom-decisions-{today_str}.csv"'
        self.assertEqual(res.headers.get("Content-Disposition"), expected_disposition)

        # CSV structure and default exclusion of skipped
        text = res.get_data(as_text=True)
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
        self.assertEqual(len(rows), 2)
        statuses = {row["status"] for row in rows}
        self.assertEqual(statuses, {"accepted", "corrected"})

        # Verify all canonical fields are present
        for field in EXPORT_FIELDS:
            self.assertIn(field, rows[0])

    def test_export_json_download_headers_and_filename(self):
        self._seed_decisions()
        res = self.client.get("/api/export?format=json", headers=self.headers())
        self.assertEqual(res.status_code, 200)

        # Content-Type verification
        self.assertEqual(res.mimetype, "application/json")
        self.assertIn("charset=utf-8", res.headers.get("Content-Type", "").lower())

        # Date-stamped Content-Disposition filename verification
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        expected_disposition = f'attachment; filename="mailroom-decisions-{today_str}.json"'
        self.assertEqual(res.headers.get("Content-Disposition"), expected_disposition)

        # JSON payload structure
        payload = json.loads(res.get_data(as_text=True))
        self.assertEqual(payload["count"], 2)
        self.assertIn("exported_at", payload)
        self.assertEqual(len(payload["decisions"]), 2)
        statuses = {d["status"] for d in payload["decisions"]}
        self.assertEqual(statuses, {"accepted", "corrected"})

    def test_export_include_skipped_toggle(self):
        self._seed_decisions()

        # CSV with include_skipped=0 vs include_skipped=1
        res_no_skipped = self.client.get("/api/export?format=csv&include_skipped=0", headers=self.headers())
        rows_no_skipped = list(csv.DictReader(io.StringIO(res_no_skipped.get_data(as_text=True))))
        self.assertEqual(len(rows_no_skipped), 2)

        res_skipped = self.client.get("/api/export?format=csv&include_skipped=1", headers=self.headers())
        rows_skipped = list(csv.DictReader(io.StringIO(res_skipped.get_data(as_text=True))))
        self.assertEqual(len(rows_skipped), 3)
        self.assertIn("skipped", {r["status"] for r in rows_skipped})

        # JSON with include_skipped=true
        res_json = self.client.get("/api/export?format=json&include_skipped=true", headers=self.headers())
        payload = json.loads(res_json.get_data(as_text=True))
        self.assertEqual(payload["count"], 3)
        self.assertIn("skipped", {d["status"] for d in payload["decisions"]})

    def test_export_neutralizes_formula_injection(self):
        acc_id = self.db.get_or_create_account(email="local", is_local=True)
        mid = self.db.upsert_message(
            acc_id,
            {
                "message_id": "msg-formula",
                "subject": "Formula test",
                "sender": "sender@example.com",
                "sender_email": "sender@example.com",
                "body_preview": "Body text",
            },
        )
        # Use validate_labels=False to simulate potential trigger injection
        self.db.save_decision(
            acc_id, mid, "corrected", ["=SUM(A1:A10)"], validate_labels=False
        )

        res = self.client.get("/api/export?format=csv", headers=self.headers())
        self.assertEqual(res.status_code, 200)
        text = res.get_data(as_text=True)
        self.assertIn("'=SUM", text)
        self.assertNotIn("\n=SUM", text)

    def test_status_reports_exportable_and_skipped_counts(self):
        # Empty DB status
        res = self.client.get("/api/status", headers=self.headers())
        self.assertEqual(res.status_code, 200)
        status_empty = res.get_json()
        self.assertEqual(status_empty["reviewed"], 0)
        self.assertEqual(status_empty["exportable"], 0)
        self.assertEqual(status_empty["skipped"], 0)

        # After seeding 1 accepted, 1 corrected, 1 skipped
        self._seed_decisions()
        res = self.client.get("/api/status", headers=self.headers())
        self.assertEqual(res.status_code, 200)
        status_data = res.get_json()
        self.assertEqual(status_data["reviewed"], 3)
        self.assertEqual(status_data["exportable"], 2)
        self.assertEqual(status_data["skipped"], 1)

    def test_export_empty_db(self):
        res_csv = self.client.get("/api/export?format=csv", headers=self.headers())
        self.assertEqual(res_csv.status_code, 200)
        reader = csv.reader(io.StringIO(res_csv.get_data(as_text=True)))
        header = next(reader)
        self.assertEqual(header, list(EXPORT_FIELDS))
        self.assertEqual(list(reader), [])

        res_json = self.client.get("/api/export?format=json", headers=self.headers())
        self.assertEqual(res_json.status_code, 200)
        payload = res_json.get_json()
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["decisions"], [])

    def test_export_host_header_enforcement(self):
        res = self.client.get("/api/export", headers={"Host": "evil.example"})
        self.assertEqual(res.status_code, 400)
