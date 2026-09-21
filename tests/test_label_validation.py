"""Tests for label definition constraints in the database decision layer (Issue #14)."""

import os
import shutil
import tempfile
import unittest

from EmailMan import app as app_module
from EmailMan import db as db_module
from EmailMan import config as config_module
from EmailMan.db import DatabaseError


class TestLabelValidation(unittest.TestCase):
    """Test label validation in DB.save_decision and custom label support."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.db = db_module.DB(self.db_path)
        self.account_id = self.db.get_or_create_account("user@example.com")
        self.message_id = self.db.upsert_message(
            self.account_id,
            {
                "gmail_message_id": "msg-001",
                "thread_id": "thread-001",
                "subject": "Test invoice",
                "sender": "Billing <billing@example.com>",
                "sender_email": "billing@example.com",
                "received_at": "2026-01-01T00:00:00",
                "body_preview": "Please find attached your invoice.",
            },
        )

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_save_decision_accepts_default_labels(self):
        decision_id = self.db.save_decision(
            self.account_id,
            self.message_id,
            "accepted",
            ["Type/Receipt", "Purchase/Tech", "Retention/Forever"],
        )
        self.assertIsNotNone(decision_id)
        decision = self.db.get_decision(self.message_id)
        self.assertEqual(
            decision["label_ids_parsed"],
            ["Type/Receipt", "Purchase/Tech", "Retention/Forever"],
        )

    def test_save_decision_rejects_unknown_labels(self):
        with self.assertRaises(DatabaseError) as ctx:
            self.db.save_decision(
                self.account_id,
                self.message_id,
                "accepted",
                ["Type/Receipt", "Bogus/UnknownLabel", "Fake/Label"],
            )
        err = str(ctx.exception)
        self.assertIn("Unknown or unconfigured label id(s)", err)
        self.assertIn("Bogus/UnknownLabel", err)
        self.assertIn("Fake/Label", err)
        self.assertIn("Allowed labels", err)

    def test_save_decision_accepts_legacy_aliases(self):
        # Retention/Keep is a legacy alias for Retention/Forever
        decision_id = self.db.save_decision(
            self.account_id,
            self.message_id,
            "corrected",
            ["Retention/Keep"],
        )
        self.assertIsNotNone(decision_id)
        decision = self.db.get_decision(self.message_id)
        self.assertEqual(decision["label_ids_parsed"], ["Retention/Keep"])

    def test_save_decision_with_custom_allowed_labels_on_db(self):
        custom_labels = ["Doc/Invoice", "Doc/Receipt", "Doc/TaxRecord"]
        custom_db = db_module.DB(
            os.path.join(self.temp_dir, "custom.db"),
            allowed_labels=custom_labels,
        )
        try:
            acc = custom_db.get_or_create_account("custom@example.com")
            c_mid = custom_db.upsert_message(
                acc,
                {
                    "gmail_message_id": "c-01",
                    "thread_id": "th-c-01",
                    "subject": "Beleg",
                    "sender": "Shop",
                    "sender_email": "shop@example.com",
                    "received_at": "2026-01-01T00:00:00",
                    "body_preview": "Ihre Rechnung",
                },
            )
            # Custom labels succeed
            custom_db.save_decision(
                acc,
                c_mid,
                "corrected",
                ["Doc/Invoice", "Doc/Receipt"],
            )
            # Default labels outside custom set fail
            with self.assertRaises(DatabaseError) as ctx:
                custom_db.save_decision(
                    acc,
                    c_mid,
                    "corrected",
                    ["Type/Receipt"],
                )
            self.assertIn("Type/Receipt", str(ctx.exception))
        finally:
            custom_db.close()

    def test_save_decision_set_allowed_labels(self):
        self.db.set_allowed_labels(["Special/Alpha", "Special/Beta"])
        # Now only Special/Alpha and Special/Beta are allowed
        self.db.save_decision(
            self.account_id,
            self.message_id,
            "corrected",
            ["Special/Alpha"],
        )
        with self.assertRaises(DatabaseError):
            self.db.save_decision(
                self.account_id,
                self.message_id,
                "corrected",
                ["Type/Receipt"],
            )

    def test_save_decision_per_call_allowed_labels_override(self):
        self.db.save_decision(
            self.account_id,
            self.message_id,
            "corrected",
            ["OneOff/Label"],
            allowed_labels=["OneOff/Label"],
        )
        stored = self.db.get_decision(self.message_id)
        self.assertEqual(stored["label_ids_parsed"], ["OneOff/Label"])

    def test_save_decision_allows_keeping_previously_saved_labels(self):
        # First save with an allowed label
        self.db.save_decision(
            self.account_id,
            self.message_id,
            "corrected",
            ["Type/Receipt"],
        )
        # Now shrink allowed labels so Type/Receipt is no longer configured
        self.db.set_allowed_labels(["Doc/Other"])

        # Updating the decision while keeping the previously saved Type/Receipt is allowed
        self.db.save_decision(
            self.account_id,
            self.message_id,
            "corrected",
            ["Type/Receipt", "Doc/Other"],
        )
        stored = self.db.get_decision(self.message_id)
        self.assertIn("Type/Receipt", stored["label_ids_parsed"])
        self.assertIn("Doc/Other", stored["label_ids_parsed"])

    def test_save_decision_allows_keeping_proposal_labels(self):
        # Message has a proposal in the DB with a label
        proposal_id = self.db.insert_proposal(
            account_id=self.account_id,
            message_id=self.message_id,
            label_ids=["Retired/InProposal"],
            reason="Suggested",
            source="model",
            model_version="test-model",
            prompt_version="test-prompt",
            label_definition_version="test-label-def",
        )
        # Even if Retired/InProposal is not in the active allowed set:
        self.db.set_allowed_labels(["Type/Receipt"])
        self.db.save_decision(
            self.account_id,
            self.message_id,
            "accepted",
            ["Retired/InProposal"],
            proposal_id=proposal_id,
        )
        stored = self.db.get_decision(self.message_id)
        self.assertEqual(stored["label_ids_parsed"], ["Retired/InProposal"])

    def test_save_decision_validate_labels_false_bypass(self):
        # Direct bypass allowed when validate_labels=False
        self.db.save_decision(
            self.account_id,
            self.message_id,
            "corrected",
            ["=cmd|' /C calc'!A0", "Wild/Nonexistent"],
            validate_labels=False,
        )
        stored = self.db.get_decision(self.message_id)
        self.assertEqual(
            stored["label_ids_parsed"],
            ["=cmd|' /C calc'!A0", "Wild/Nonexistent"],
        )

    def test_config_get_label_ids_and_custom_labels(self):
        cfg = config_module.Config(
            os.path.join(self.temp_dir, "config.json"),
            labels=[
                {
                    "id": "Doc/Invoice",
                    "name": "Invoice",
                    "description": "Bill for services or goods",
                    "examples": ["Amazon invoice", "Hosting bill"],
                    "exclusions": ["Shipping update"],
                }
            ],
        )
        self.assertEqual(cfg.get_label_ids(), ["Doc/Invoice"])
        self.assertEqual(len(cfg.labels), 1)
        self.assertEqual(cfg.labels[0]["id"], "Doc/Invoice")


class TestRouteLabelValidation(unittest.TestCase):
    """Test that routes delegate label validation to DB and return 400 on violations."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.temp_dir, "config.json")
        self.config = config_module.Config(self.config_path)
        self.db = db_module.DB(self.config.database_path)
        self.account_id = self.db.get_or_create_account("user@example.com")
        self.message_id = self.db.upsert_message(
            self.account_id,
            {
                "gmail_message_id": "msg-001",
                "thread_id": "thread-001",
                "subject": "Test invoice",
                "sender": "Billing <billing@example.com>",
                "sender_email": "billing@example.com",
                "received_at": "2026-01-01T00:00:00",
                "body_preview": "Please find attached your invoice.",
            },
        )
        self.db.close()
        self.app = app_module.create_app(self.config)
        self.client = self.app.test_client()
        self.csrf = self.app.config["CSRF_TOKEN"]

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_api_save_decision_rejects_unknown_labels_via_db(self):
        res = self.client.post(
            "/api/decisions",
            json={
                "message_id": self.message_id,
                "status": "corrected",
                "label_ids": ["NonExistent/Label"],
            },
            headers={"X-CSRF-Token": self.csrf, "Origin": "http://127.0.0.1:5000"},
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("Unknown or unconfigured label id(s)", res.get_json()["error"])

    def test_api_conflicts_resolve_rejects_unknown_labels_via_db(self):
        res = self.client.post(
            "/api/conflicts/resolve",
            json={
                "message_ids": [self.message_id],
                "label_ids": ["NonExistent/ConflictLabel"],
            },
            headers={"X-CSRF-Token": self.csrf, "Origin": "http://127.0.0.1:5000"},
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("Unknown or unconfigured label id(s)", res.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
