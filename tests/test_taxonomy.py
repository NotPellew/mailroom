"""Tests for label vocabulary discovery (local model, no Gmail writes)."""

import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import MagicMock, patch

from EmailMan import cli as cli_module
from EmailMan import config as config_module
from EmailMan import db as db_module
from EmailMan import taxonomy as taxonomy_module



class TestTaxonomyHelpers(unittest.TestCase):
    def test_coerce_and_cap_purchase_labels(self):
        parsed = {
            "labels": [
                {"id": "Type/Newsletter", "name": "Newsletter", "axis": "kind", "retention": "ephemeral"},
                {"id": "Purchase/FoodDrink", "name": "Food", "axis": "purchase", "retention": "keep"},
            ]
            + [
                {
                    "id": f"Purchase/Extra{i}",
                    "name": f"E{i}",
                    "axis": "purchase",
                    "retention": "keep",
                }
                for i in range(10)
            ]
        }
        labels = taxonomy_module._coerce_labels(parsed)
        capped = taxonomy_module._cap_purchase_labels(labels)
        purchase = [item for item in capped if item["axis"] == "purchase"]
        self.assertLessEqual(len(purchase), taxonomy_module.MAX_PURCHASE_LABELS)
        self.assertTrue(any(item["id"] == "Type/Newsletter" for item in capped))

    def test_coerced_labels_are_valid_config_labels(self):
        """Finding 8: suggested labels carry exclusions/description for config."""
        parsed = {
            "labels": [
                {
                    "id": "Type/Newsletter",
                    "name": "Newsletter",
                    "axis": "kind",
                    "retention": "ephemeral",
                },
                {
                    "id": "Type/Targeted",
                    "name": "Targeted",
                    "axis": "kind",
                    "retention": "review",
                },
            ]
        }
        labels = taxonomy_module._coerce_labels(parsed)
        self.assertEqual(len(labels), 2)
        for label in labels:
            self.assertIn("exclusions", label)
            self.assertIsInstance(label["exclusions"], list)
            self.assertTrue(label["description"])

        config = config_module.Config(os.path.join(self._tmpdir(), "config.json"))
        config._config["labels"] = labels
        config.validate()  # must not raise

    @staticmethod
    def _tmpdir():
        import tempfile

        return tempfile.mkdtemp()

    def test_suggest_label_vocabulary_batches_then_merges(self):
        client = MagicMock()
        client.complete_json.side_effect = [
            {"labels": [{"id": "Type/Newsletter", "name": "NL", "axis": "kind", "retention": "ephemeral"}]},
            {"labels": [{"id": "Type/Targeted", "name": "T", "axis": "kind", "retention": "review"}]},
            {
                "labels": [
                    {"id": "Type/Newsletter", "name": "Newsletter", "axis": "kind", "retention": "ephemeral"},
                    {"id": "Type/Targeted", "name": "Targeted", "axis": "kind", "retention": "review"},
                ]
            },
        ]
        messages = [
            {"sender": "A", "sender_email": "a@a", "subject": "s1", "body_preview": "b1"},
            {"sender": "B", "sender_email": "b@b", "subject": "s2", "body_preview": "b2"},
        ]
        result = taxonomy_module.suggest_label_vocabulary(client, messages, batch_size=1)
        self.assertEqual(result["message_count"], 2)
        self.assertEqual(result["batch_count"], 2)
        self.assertEqual(client.complete_json.call_count, 3)
        ids = {item["id"] for item in result["labels"]}
        self.assertEqual(ids, {"Type/Newsletter", "Type/Targeted"})


class TestSuggestLabelsCli(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.temp_dir, "config.json")
        self.config = config_module.Config(self.config_path)
        db = db_module.DB(self.config.database_path)
        account = db.get_or_create_account("user@example.com")
        db.upsert_message(
            account,
            {
                "gmail_message_id": "gm-1",
                "thread_id": "th-1",
                "subject": "Weekly digest",
                "sender": "News",
                "sender_email": "n@example.com",
                "received_at": "2026-01-01T00:00:00",
                "body_preview": "Hello subscriber",
                "gmail_labels": [],
                "truncated": False,
                "unsupported_content": False,
                "has_attachments": False,
            },
        )
        db.close()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_cli_writes_timestamped_and_latest_without_clobbering_prior(self):
        args = cli_module.create_cli_parser().parse_args(
            ["--config", self.config_path, "suggest-labels", "--limit", "10"]
        )
        fake = {
            "labels": [
                {
                    "id": "Type/Newsletter",
                    "name": "Newsletter",
                    "axis": "kind",
                    "retention": "ephemeral",
                    "description": "digest",
                    "examples": ["Weekly digest"],
                }
            ],
            "message_count": 1,
            "batch_count": 1,
        }
        with patch("EmailMan.taxonomy.TaxonomyStream.finish", return_value=fake):
            first = cli_module.suggest_labels_cmd(args, self.config)
            second = cli_module.suggest_labels_cmd(args, self.config)
        self.assertEqual(first, 0)
        self.assertEqual(second, 0)
        stamped = [
            name
            for name in os.listdir(self.temp_dir)
            if name.startswith("suggested-labels-") and name != "suggested-labels-latest.json"
        ]
        self.assertGreaterEqual(len(stamped), 2)
        latest = os.path.join(self.temp_dir, "suggested-labels-latest.json")
        self.assertTrue(os.path.exists(latest))
        payload = json.loads(open(latest, encoding="utf-8").read())
        self.assertEqual(payload["labels"][0]["id"], "Type/Newsletter")
        self.assertIn("model_id", payload)
        self.assertIn("created_at", payload)


def _scan_decoded(i, body=None):
    return {
        "gmail_message_id": f"gm-{i}",
        "thread_id": f"th-{i}",
        "subject": f"Subject {i}",
        "sender": "Sender",
        "sender_email": "s@example.com",
        "received_at": "2026-01-01T00:00:00",
        "body_preview": body if body is not None else f"secret-body-{i}",
        "gmail_labels": [],
        "truncated": False,
        "unsupported_content": False,
        "has_attachments": False,
    }


class TestScanSuggestLabels(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.temp_dir, "config.json")
        self.config = config_module.Config(self.config_path)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _args(self, extra):
        return cli_module.create_cli_parser().parse_args(
            ["--config", self.config_path, "scan", "--limit", "20"] + extra
        )

    def test_classify_and_suggest_labels_are_exclusive(self):
        args = self._args(["--classify", "--suggest-labels"])
        code = cli_module.scan_cmd(args, self.config)
        self.assertEqual(code, 1)

    def test_on_message_batch_runs_before_fetch_returns(self):
        calls = []

        def complete_json(prompt, max_tokens=1500):
            calls.append("merge" if "Batch proposals:" in prompt else "batch")
            return {
                "labels": [
                    {
                        "id": "Type/Newsletter",
                        "name": "Newsletter",
                        "axis": "kind",
                        "retention": "ephemeral",
                    }
                ]
            }

        seen_during_fetch = []

        def fake_fetch(service, query=None, limit=None, skip_ids=None, on_message=None, on_skip=None):
            for i in range(8):
                on_message(_scan_decoded(i))
            deadline = time.time() + 2
            while time.time() < deadline and "batch" not in calls:
                time.sleep(0.01)
            seen_during_fetch.append(list(calls))
            return [], "2026-01-01T00:00:00Z"

        with patch("EmailMan.gmail.get_gmail_service", return_value=(MagicMock(), "user@example.com")), patch(
            "EmailMan.gmail.fetch_account_and_labels", return_value=({}, [])
        ), patch("EmailMan.gmail.fetch_bounded_sample", side_effect=fake_fetch), patch(
            "EmailMan.classification.TabbyClient.complete_json", side_effect=complete_json
        ):
            args = self._args(["--suggest-labels", "--batch-size", "8"])
            code = cli_module.scan_cmd(args, self.config)
        self.assertEqual(code, 0)
        self.assertIn("batch", seen_during_fetch[0])
        self.assertTrue(os.path.exists(os.path.join(self.temp_dir, "suggested-labels-latest.json")))

    def test_leftover_and_merge_after_sentinel(self):
        prompts = []

        def complete_json(prompt, max_tokens=1500):
            kind = "merge" if "Batch proposals:" in prompt else "batch"
            prompts.append(kind)
            return {
                "labels": [
                    {"id": "Type/Targeted", "name": "Targeted", "axis": "kind", "retention": "review"}
                ]
            }

        def fake_fetch(service, query=None, limit=None, skip_ids=None, on_message=None, on_skip=None):
            for i in range(9):
                on_message(_scan_decoded(i))
            return [], "t"

        with patch("EmailMan.gmail.get_gmail_service", return_value=(MagicMock(), "user@example.com")), patch(
            "EmailMan.gmail.fetch_account_and_labels", return_value=({}, [])
        ), patch("EmailMan.gmail.fetch_bounded_sample", side_effect=fake_fetch), patch(
            "EmailMan.classification.TabbyClient.complete_json", side_effect=complete_json
        ):
            args = self._args(["--suggest-labels", "--batch-size", "8"])
            self.assertEqual(cli_module.scan_cmd(args, self.config), 0)
        self.assertEqual(prompts, ["batch", "batch", "merge"])

    def test_quota_partial_fetch_still_writes_report(self):
        from EmailMan.gmail import GmailError

        def complete_json(prompt, max_tokens=1500):
            return {
                "labels": [
                    {"id": "Type/Newsletter", "name": "Newsletter", "axis": "kind", "retention": "ephemeral"}
                ]
            }

        def fake_fetch(service, query=None, limit=None, skip_ids=None, on_message=None, on_skip=None):
            on_message(_scan_decoded(0))
            on_message(_scan_decoded(1))
            raise GmailError("quota")

        buf = io.StringIO()
        err = io.StringIO()
        with patch("EmailMan.gmail.get_gmail_service", return_value=(MagicMock(), "user@example.com")), patch(
            "EmailMan.gmail.fetch_account_and_labels", return_value=({}, [])
        ), patch("EmailMan.gmail.fetch_bounded_sample", side_effect=fake_fetch), patch(
            "EmailMan.classification.TabbyClient.complete_json", side_effect=complete_json
        ), redirect_stdout(buf), redirect_stderr(err):
            args = self._args(["--suggest-labels", "--batch-size", "8"])
            code = cli_module.scan_cmd(args, self.config)
        self.assertEqual(code, 1)
        latest = os.path.join(self.temp_dir, "suggested-labels-latest.json")
        self.assertTrue(os.path.exists(latest))
        payload = json.loads(open(latest, encoding="utf-8").read())
        self.assertTrue(payload.get("partial"))
        self.assertGreaterEqual(payload["message_count"], 1)
        combined = buf.getvalue() + err.getvalue()
        self.assertNotIn("secret-body", combined)
        self.assertIn("taxonomy batch", combined.lower())

    def test_cached_skip_still_feeds_suggest_labels(self):
        calls = []

        def complete_json(prompt, max_tokens=1500):
            calls.append(prompt)
            return {
                "labels": [
                    {"id": "Type/Newsletter", "name": "Newsletter", "axis": "kind", "retention": "ephemeral"}
                ]
            }

        def fake_fetch(service, query=None, limit=None, skip_ids=None, on_message=None, on_skip=None):
            if on_skip:
                on_skip("gm-1")
            return [], "t"

        db = db_module.DB(self.config.database_path)
        account = db.get_or_create_account("user@example.com")
        db.upsert_message(account, _scan_decoded(1, body="cached body text"))
        db.close()

        with patch("EmailMan.gmail.get_gmail_service", return_value=(MagicMock(), "user@example.com")), patch(
            "EmailMan.gmail.fetch_account_and_labels", return_value=({}, [])
        ), patch("EmailMan.gmail.fetch_bounded_sample", side_effect=fake_fetch), patch(
            "EmailMan.classification.TabbyClient.complete_json", side_effect=complete_json
        ):
            args = self._args(["--suggest-labels", "--batch-size", "8"])
            code = cli_module.scan_cmd(args, self.config)
        self.assertEqual(code, 0)
        self.assertTrue(calls)
        payload = json.loads(open(os.path.join(self.temp_dir, "suggested-labels-latest.json"), encoding="utf-8").read())
        self.assertGreaterEqual(payload["message_count"], 1)

    def test_merge_timeout_falls_back_to_local_union(self):
        from EmailMan.classification import ClassificationError

        client = MagicMock()
        client.complete_json.side_effect = ClassificationError("Classification timeout after 3 attempts")
        drafts = [
            [{"id": "Type/Newsletter", "name": "NL", "axis": "kind", "retention": "ephemeral", "examples": ["a"]}],
            [{"id": "Type/Targeted", "name": "T", "axis": "kind", "retention": "review", "examples": ["b"]}],
        ]
        merged = taxonomy_module.merge_taxonomy_drafts(client, drafts)
        ids = {item["id"] for item in merged}
        self.assertEqual(ids, {"Type/Newsletter", "Type/Targeted"})

    def test_fallback_collapses_aliases_and_drops_noise(self):
        drafts = [
            [
                {"id": "Type/VerificationCode", "name": "Code", "axis": "kind", "retention": "ephemeral", "examples": ["otp"]},
                {"id": "Type/Marketing", "name": "Mkt", "axis": "kind", "retention": "ephemeral", "examples": ["deal"]},
                {"id": "Type/CIAlert", "name": "CI", "axis": "kind", "retention": "ephemeral", "examples": ["build"]},
                {"id": "Purchase/Apparel", "name": "Apparel", "axis": "purchase", "retention": "keep", "examples": ["shirt"]},
            ]
        ]
        merged = taxonomy_module.fallback_merge_drafts(drafts)
        ids = {item["id"] for item in merged}
        self.assertIn("Type/Verification", ids)
        self.assertIn("Type/Newsletter", ids)
        self.assertIn("Purchase/Clothing", ids)
        self.assertNotIn("Type/CIAlert", ids)
        self.assertNotIn("Type/Marketing", ids)

    def test_failed_batch_is_skipped_and_merge_uses_successes(self):
        client = MagicMock()
        client.complete_json.side_effect = [
            RuntimeError("model down"),
            {"labels": [{"id": "Type/Newsletter", "name": "NL", "axis": "kind", "retention": "ephemeral"}]},
        ]
        stream = taxonomy_module.TaxonomyStream(client, batch_size=1)
        stream.add(_scan_decoded(0))
        stream.add(_scan_decoded(1))
        result = stream.finish()
        self.assertEqual(result["failed_batches"], 1)
        self.assertEqual(result["labels"][0]["id"], "Type/Newsletter")
