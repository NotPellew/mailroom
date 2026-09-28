"""End-to-end tests for in-app batch classification and pending list APIs."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from Mailroom import app as app_module
from Mailroom.classification import Proposal
from Mailroom.config import Config
from Mailroom.db import DB


class WebClassifyApiTest(unittest.TestCase):
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

    def _insert_message(self, message_id: str, subject: str, body: str) -> str:
        acc_id = self.db.get_or_create_account(email="local", is_local=True)
        return self.db.upsert_message(
            acc_id,
            {
                "message_id": message_id,
                "subject": subject,
                "sender": "sender@example.com",
                "sender_email": "sender@example.com",
                "body_preview": body,
            },
        )

    def test_pending_empty(self):
        res = self.client.get("/api/classify/pending", headers=self.headers())
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["pending"], [])

    def test_pending_filtering(self):
        # 1. Message with body, no proposal -> should be pending
        id1 = self._insert_message("msg-pending-1", "Subject 1", "Body text 1")
        # 2. Message with empty body, no proposal -> should NOT be pending
        self._insert_message("msg-no-body", "Subject 2", "")
        # 3. Message with body, already has proposal -> should NOT be pending
        id3 = self._insert_message("msg-classified", "Subject 3", "Body text 3")
        self.db.insert_proposal(
            account_id="local",
            message_id=id3,
            label_ids=["Type/Personal"],
            label_names=["Personal"],
            reason="reason",
            source="model",
            model_version="test-model",
            prompt_version="p1",
            label_definition_version="ld1",
            confidence=0.9,
            abstain=False,
        )

        res = self.client.get("/api/classify/pending", headers=self.headers())
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["count"], 1)
        self.assertEqual(len(data["pending"]), 1)
        self.assertEqual(data["pending"][0]["id"], id1)
        self.assertEqual(data["pending"][0]["subject"], "Subject 1")
        # Ensure body preview is NOT leaked in pending metadata
        self.assertNotIn("body_preview", data["pending"][0])

    def test_pending_limit_and_clamping(self):
        for i in range(5):
            self._insert_message(f"msg-batch-{i}", f"Subject {i}", f"Body {i}")

        # Limit 2
        res = self.client.get("/api/classify/pending?limit=2", headers=self.headers())
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["total"], 5)
        self.assertEqual(data["count"], 2)
        self.assertEqual(len(data["pending"]), 2)

        # Invalid limit string falls back safely
        res_invalid = self.client.get("/api/classify/pending?limit=invalid", headers=self.headers())
        self.assertEqual(res_invalid.status_code, 200)
        self.assertEqual(res_invalid.get_json()["count"], 5)

        # Negative limit clamped to 1
        res_neg = self.client.get("/api/classify/pending?limit=-10", headers=self.headers())
        self.assertEqual(res_neg.status_code, 200)
        self.assertEqual(res_neg.get_json()["count"], 1)

    def test_status_reports_unclassified_count(self):
        status_res1 = self.client.get("/api/status", headers=self.headers())
        self.assertEqual(status_res1.status_code, 200)
        self.assertEqual(status_res1.get_json()["unclassified"], 0)

        self._insert_message("msg-unclassified-1", "Subject", "Body preview text")

        status_res2 = self.client.get("/api/status", headers=self.headers())
        self.assertEqual(status_res2.status_code, 200)
        self.assertEqual(status_res2.get_json()["unclassified"], 1)

    @patch("Mailroom.classification.classify_message")
    def test_end_to_end_classify_flow(self, mock_classify):
        mock_classify.return_value = Proposal(
            label_ids=["Type/Personal"],
            reason="Clear personal email",
            confidence=0.95,
            abstain=False,
        )

        msg_id = self._insert_message("flow-test-1", "Flow Subject", "Hello from flow")

        # 1. Message appears in pending
        pending_res1 = self.client.get("/api/classify/pending", headers=self.headers())
        self.assertEqual(pending_res1.get_json()["total"], 1)

        # 2. Call POST /api/classify to classify
        classify_res = self.client.post(
            "/api/classify",
            data=json.dumps({"message_id": msg_id}),
            content_type="application/json",
            headers=self.headers(),
        )
        self.assertEqual(classify_res.status_code, 200)
        payload = classify_res.get_json()
        self.assertEqual(payload["label_ids"], ["Type/Personal"])
        self.assertIn("proposal_id", payload)

        # 3. Message is no longer pending
        pending_res2 = self.client.get("/api/classify/pending", headers=self.headers())
        self.assertEqual(pending_res2.get_json()["total"], 0)
        self.assertEqual(pending_res2.get_json()["pending"], [])

        # 4. Status reflects proposal
        status_res = self.client.get("/api/status", headers=self.headers())
        status_data = status_res.get_json()
        self.assertEqual(status_data["unclassified"], 0)
        self.assertEqual(status_data["proposals_pending"], 1)

    def test_pending_host_header_enforcement(self):
        res = self.client.get(
            "/api/classify/pending",
            headers={"Host": "evil.com"},
        )
        self.assertEqual(res.status_code, 400)


if __name__ == "__main__":
    unittest.main()
