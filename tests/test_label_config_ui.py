"""Issue #23: label definitions are editable from the review web UI.

Covers the atomic config write path (``Config.set_labels``), the ``/api/labels``
mutation endpoints, in-use removal refusal, and live refresh without a restart.
Gmail is never involved: these tests need no credentials or optional extras.
"""

import json
import os
import shutil
import tempfile
import unittest
from urllib.parse import quote

from Mailroom import app as app_module
from Mailroom import config as config_module
from Mailroom import db as db_module
from Mailroom.config import ConfigError

BACKUP_SUFFIX = ".bak-label-edit"


def make_label(label_id="Custom/Test", name="Test label", axis="kind",
               description="A label used by the tests.",
               examples=None, exclusions=None):
    return {
        "id": label_id,
        "name": name,
        "axis": axis,
        "description": description,
        "examples": ["Example one"] if examples is None else examples,
        "exclusions": ["Exclusion one"] if exclusions is None else exclusions,
    }


class ConfigTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.temp_dir, "config.json")
        self.config = config_module.Config(self.config_path)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def read_config_bytes(self):
        with open(self.config_path, "rb") as handle:
            return handle.read()

    @property
    def backup_path(self):
        return self.config_path + BACKUP_SUFFIX


class TestSetLabels(ConfigTestBase):
    def test_set_labels_persists_live_and_backs_up(self):
        before = self.read_config_bytes()

        updated = [make_label()]
        self.config.set_labels(updated)

        # Live instance reflects the change immediately.
        self.assertEqual(self.config.labels, updated)
        # File on disk is the new valid JSON.
        with open(self.config_path, "r", encoding="utf-8") as handle:
            on_disk = json.load(handle)
        self.assertEqual(on_disk["labels"], updated)
        # Rolling backup keeps the previous bytes.
        self.assertTrue(os.path.exists(self.backup_path))
        with open(self.backup_path, "rb") as handle:
            self.assertEqual(handle.read(), before)

    def test_set_labels_rejection_leaves_file_and_memory_untouched(self):
        original_labels = self.config.labels
        before = self.read_config_bytes()

        duplicate_id = [make_label(label_id="A/One", name="One"),
                        make_label(label_id="A/One", name="Two")]
        duplicate_name = [make_label(label_id="A/One", name="Same"),
                          make_label(label_id="A/Two", name="Same")]
        missing_description = [make_label()]
        missing_description[0].pop("description")
        non_string_id = [make_label()]
        non_string_id[0]["id"] = 123

        cases = {
            "duplicate id": duplicate_id,
            "duplicate name": duplicate_name,
            "missing required field": missing_description,
            "empty list": [],
            "non-string id": non_string_id,
        }
        for name, labels in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(ConfigError):
                    self.config.set_labels(labels)
                self.assertEqual(self.config.labels, original_labels)
                self.assertEqual(self.read_config_bytes(), before)
                self.assertFalse(os.path.exists(self.backup_path))

    def test_rejection_without_labels_key_restores_defaults(self):
        # A config may legitimately have no "labels" key; a rejected edit must
        # not materialize a `labels: null` key (which would suppress defaults).
        del self.config._config["labels"]
        before = self.read_config_bytes()

        with self.assertRaises(ConfigError):
            self.config.set_labels([make_label(label_id=123)])

        self.assertNotIn("labels", self.config._config)
        self.assertEqual(self.config.labels, self.config.DEFAULT_LABELS)
        self.assertEqual(self.read_config_bytes(), before)

    def test_write_failure_restores_memory_and_file(self):
        # validate() accepts extra keys, but a non-serializable extra value makes
        # json.dump raise after the backup step. The live list must be restored.
        previous = self.config.labels
        before = self.read_config_bytes()
        broken = make_label(label_id="Custom/Broken", name="Broken")
        broken["extra"] = object()

        with self.assertRaises(TypeError):
            self.config.set_labels([broken])

        self.assertEqual(self.config.labels, previous)
        self.assertEqual(self.read_config_bytes(), before)


class LabelApiTestBase(ConfigTestBase):
    def setUp(self):
        super().setUp()
        self.app = app_module.create_app(self.config)
        self.client = self.app.test_client()
        self.csrf = self.app.config["CSRF_TOKEN"]

    def headers(self, csrf=True, origin="http://127.0.0.1:5000"):
        headers = {}
        if origin is not None:
            headers["Origin"] = origin
        if csrf:
            headers["X-CSRF-Token"] = self.csrf
        return headers

    def add_label_via_db_decision(self, label_ids):
        db_obj = db_module.DB(self.config.database_path)
        try:
            account_id = db_obj.get_or_create_account("user@example.com")
            message_id = db_obj.upsert_message(
                account_id,
                {
                    "gmail_message_id": "ui-msg-1",
                    "thread_id": "ui-thread-1",
                    "subject": "Subject",
                    "sender": "Sender <sender@example.com>",
                    "sender_email": "sender@example.com",
                    "received_at": "2026-01-01T00:00:00",
                    "body_preview": "Body",
                },
            )
            db_obj.save_decision(account_id, message_id, "corrected", label_ids)
            return message_id
        finally:
            db_obj.close()

    def decision_labels(self, message_id):
        db_obj = db_module.DB(self.config.database_path)
        try:
            return db_obj.get_decision(message_id)["label_ids_parsed"]
        finally:
            db_obj.close()


class TestLabelApi(LabelApiTestBase):
    def test_create_label_appears_and_persists(self):
        payload = make_label(label_id="Custom/Created", name="Created")
        res = self.client.post("/api/labels", json=payload, headers=self.headers())
        self.assertEqual(res.status_code, 200)

        listed = {label["id"] for label in self.client.get("/api/labels").get_json()["labels"]}
        self.assertIn("Custom/Created", listed)
        with open(self.config_path, "r", encoding="utf-8") as handle:
            on_disk = json.load(handle)
        self.assertIn("Custom/Created", {label["id"] for label in on_disk["labels"]})

    def test_update_label_fields_persisted(self):
        updated = make_label(
            label_id="Type/Receipt",
            name="Receipt updated",
            axis="retention",
            description="Updated description",
            examples=["Example A", "Example B"],
            exclusions=["Exclusion A"],
        )
        res = self.client.put("/api/labels/Type/Receipt", json=updated, headers=self.headers())
        self.assertEqual(res.status_code, 200)

        by_id = {label["id"]: label for label in self.client.get("/api/labels").get_json()["labels"]}
        self.assertEqual(by_id["Type/Receipt"]["name"], "Receipt updated")
        self.assertEqual(by_id["Type/Receipt"]["axis"], "retention")
        self.assertEqual(by_id["Type/Receipt"]["description"], "Updated description")
        self.assertEqual(by_id["Type/Receipt"]["examples"], ["Example A", "Example B"])
        self.assertEqual(by_id["Type/Receipt"]["exclusions"], ["Exclusion A"])
        with open(self.config_path, "r", encoding="utf-8") as handle:
            on_disk = json.load(handle)
        self.assertEqual(
            {label["id"]: label for label in on_disk["labels"]}["Type/Receipt"]["name"],
            "Receipt updated",
        )

    def test_update_changing_id_refused(self):
        before = self.read_config_bytes()
        payload = make_label(label_id="Type/Changed", name="Receipt")
        res = self.client.put("/api/labels/Type/Receipt", json=payload, headers=self.headers())
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.read_config_bytes(), before)

    def test_update_unknown_id_returns_404(self):
        res = self.client.put(
            "/api/labels/No/Such", json=make_label(), headers=self.headers()
        )
        self.assertEqual(res.status_code, 404)

    def test_delete_unknown_id_returns_404(self):
        res = self.client.delete("/api/labels/No/Such", headers=self.headers())
        self.assertEqual(res.status_code, 404)

    def test_delete_unreferenced_removes_label(self):
        res = self.client.delete("/api/labels/Type/ShippingUpdate", headers=self.headers())
        self.assertEqual(res.status_code, 200)
        listed = {label["id"] for label in res.get_json()["labels"]}
        self.assertNotIn("Type/ShippingUpdate", listed)

    def test_update_and_get_with_percent_encoded_id(self):
        # The browser UI sends encodeURIComponent(id); "%2F" must route the same.
        encoded = quote("Type/Receipt", safe="")
        payload = make_label(label_id="Type/Receipt", name="Receipt encoded")
        res = self.client.put("/api/labels/" + encoded, json=payload, headers=self.headers())
        self.assertEqual(res.status_code, 200)

        listed = {
            label["id"]: label for label in self.client.get("/api/labels").get_json()["labels"]
        }
        self.assertEqual(listed["Type/Receipt"]["name"], "Receipt encoded")

    def test_delete_with_percent_encoded_id(self):
        encoded = quote("Type/ShippingUpdate", safe="")
        res = self.client.delete("/api/labels/" + encoded, headers=self.headers())
        self.assertEqual(res.status_code, 200)
        listed = {label["id"] for label in res.get_json()["labels"]}
        self.assertNotIn("Type/ShippingUpdate", listed)

    def test_delete_in_use_refused(self):
        message_id = self.add_label_via_db_decision(["Type/Receipt"])
        before = self.read_config_bytes()

        res = self.client.delete("/api/labels/Type/Receipt", headers=self.headers())
        self.assertEqual(res.status_code, 400)
        body = res.get_json()
        self.assertEqual(body["in_use"], 1)
        self.assertIn("saved decision", body["error"])

        self.assertEqual(self.read_config_bytes(), before)
        self.assertEqual(self.decision_labels(message_id), ["Type/Receipt"])
        listed = {label["id"] for label in self.client.get("/api/labels").get_json()["labels"]}
        self.assertIn("Type/Receipt", listed)

    def test_delete_in_use_counts_legacy_alias(self):
        message_id = self.add_label_via_db_decision(["Retention/Keep"])
        before = self.read_config_bytes()

        res = self.client.delete("/api/labels/Retention/Forever", headers=self.headers())
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["in_use"], 1)
        self.assertEqual(self.read_config_bytes(), before)
        self.assertEqual(self.decision_labels(message_id), ["Retention/Keep"])

    def test_validation_errors_via_api_leave_config_unchanged(self):
        before = self.read_config_bytes()
        duplicate_id = make_label(label_id="Type/Receipt", name="Another name")
        duplicate_name = make_label(label_id="Custom/New", name="Receipt")
        missing_description = make_label(label_id="Custom/New")
        missing_description.pop("description")

        for name, payload in (
            ("duplicate id", duplicate_id),
            ("duplicate name", duplicate_name),
            ("missing description", missing_description),
        ):
            with self.subTest(case=name):
                res = self.client.post("/api/labels", json=payload, headers=self.headers())
                self.assertEqual(res.status_code, 400)
                self.assertIn("error", res.get_json())
                self.assertEqual(self.read_config_bytes(), before)

    def test_deleting_last_label_is_rejected_as_empty_taxonomy(self):
        self.config.set_labels([make_label(label_id="Only/One", name="Only")])
        before = self.read_config_bytes()

        res = self.client.delete("/api/labels/Only/One", headers=self.headers())
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.read_config_bytes(), before)

    def test_mutation_without_csrf_is_forbidden(self):
        payload = make_label(label_id="Custom/Created", name="Created")
        res = self.client.post("/api/labels", json=payload, headers=self.headers(csrf=False))
        self.assertEqual(res.status_code, 403)

    def test_non_loopback_host_is_rejected(self):
        res = self.client.get("/api/labels", headers={"Host": "evil.example"})
        self.assertEqual(res.status_code, 400)

    def test_live_refresh_without_restart(self):
        payload = make_label(label_id="Custom/Live", name="Live")
        res = self.client.post("/api/labels", json=payload, headers=self.headers())
        self.assertEqual(res.status_code, 200)

        # Same app instance, no restart between the write and the read.
        listed = {label["id"] for label in self.client.get("/api/labels").get_json()["labels"]}
        self.assertIn("Custom/Live", listed)


if __name__ == "__main__":
    unittest.main()
