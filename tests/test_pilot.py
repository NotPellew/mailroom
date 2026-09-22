"""Step 5 pilot metrics and similar-phrase label conflicts."""

import os
import tempfile
import unittest

from Mailroom import app as app_module
from Mailroom import config as config_module
from Mailroom import db as db_module
from Mailroom import pilot as pilot_module


class TestPilotConflicts(unittest.TestCase):
    def test_normalize_subject_strips_re_fwd(self):
        self.assertEqual(
            pilot_module.normalize_subject("Re: Re: Your receipt"),
            "your receipt",
        )
        self.assertEqual(pilot_module.normalize_subject("FWD: Hello"), "hello")

    def test_same_subject_different_labels_is_a_conflict(self):
        rows = [
            {
                "status": "corrected",
                "human": ["Type/Newsletter", "Retention/30Days"],
                "subject": "Weekly digest",
                "sender_email": "a@b.com",
            },
            {
                "status": "corrected",
                "human": ["Type/Targeted", "Retention/Forever"],
                "subject": "RE: Weekly digest",
                "sender_email": "a@b.com",
            },
            {
                "status": "corrected",
                "human": ["Type/Receipt"],
                "subject": "Something unique invoice 999",
                "sender_email": "c@d.com",
            },
        ]
        found = pilot_module.find_label_conflicts(rows)
        self.assertEqual(found["n_groups"], 1)
        self.assertEqual(found["n_messages"], 2)
        self.assertEqual(found["groups"][0]["phrase"], "weekly digest")

    def test_metrics_from_db_decisions(self):
        temp = tempfile.mkdtemp()
        try:
            cfg = config_module.Config(os.path.join(temp, "config.json"))
            db = db_module.DB(cfg.database_path)
            acc = db.get_or_create_account("user@example.com")
            for i, (subj, human, model, status) in enumerate(
                [
                    ("Alpha", ["Type/Receipt"], ["Type/Receipt"], "accepted"),
                    ("Beta", ["Type/Newsletter"], ["Type/Targeted"], "corrected"),
                    ("Beta", ["Type/Receipt"], ["Type/Newsletter"], "corrected"),
                ]
            ):
                gid = f"g{i}"
                db.upsert_message(
                    acc,
                    {
                        "gmail_message_id": gid,
                        "thread_id": gid,
                        "subject": subj,
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
                mid = f"{acc}:{gid}"
                db.insert_proposal(acc, mid, model, "r", "model", "m", "p", "l")
                db.save_decision(acc, mid, status, human)
            metrics = pilot_module.compute_pilot_metrics(
                db, ["Type/Receipt", "Type/Newsletter", "Type/Targeted"], 0.0
            )
            db.close()
            self.assertEqual(metrics["n_reviewed_total"], 3)
            self.assertEqual(metrics["all_reviewed"]["n_accepted"], 1)
            self.assertEqual(metrics["all_reviewed"]["n_corrected"], 2)
            self.assertGreaterEqual(metrics["conflicts"]["n_groups"], 1)
            self.assertIn("members", metrics["conflicts"]["groups"][0])
        finally:
            import shutil

            shutil.rmtree(temp, ignore_errors=True)

    def test_metrics_can_score_the_latest_proposal(self):
        """prefer_latest scores a reclassification against frozen human labels."""
        temp = tempfile.mkdtemp()
        try:
            cfg = config_module.Config(os.path.join(temp, "config.json"))
            db = db_module.DB(cfg.database_path)
            acc = db.get_or_create_account("user@example.com")
            db.upsert_message(
                acc,
                {
                    "gmail_message_id": "g0",
                    "thread_id": "g0",
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
            mid = f"{acc}:g0"
            # The decision was made against the old (basis) proposal.
            db.insert_proposal(acc, mid, ["Type/Newsletter"], "old", "model", "m", "p1", "l1")
            db.save_decision(acc, mid, "corrected", ["Type/Receipt"])
            # Reclassification later adds a newer proposal.
            db.insert_proposal(acc, mid, ["Type/Receipt"], "new", "model", "m", "p2", "l2")

            allowed = ["Type/Receipt", "Type/Newsletter"]
            basis = pilot_module.compute_pilot_metrics(db, allowed, 0.0)
            latest = pilot_module.compute_pilot_metrics(db, allowed, 0.0, prefer_latest=True)
            db.close()

            self.assertEqual(basis["all_reviewed"]["per_label"]["Type/Receipt"]["recall"], 0.0)
            self.assertEqual(latest["all_reviewed"]["per_label"]["Type/Receipt"]["recall"], 1.0)
            self.assertEqual(latest["all_reviewed"]["per_label"]["Type/Newsletter"]["predicted"], 0)
        finally:
            import shutil

            shutil.rmtree(temp, ignore_errors=True)


class TestConflictsApi(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config = config_module.Config(os.path.join(self.temp_dir, "config.json"))
        self.db = db_module.DB(self.config.database_path)
        self.account = self.db.get_or_create_account("user@example.com")
        for i, labels in enumerate((["Type/Verification"], ["Type/Verification", "Type/SecurityAlert"])):
            gid = "otp-%d" % i
            self.db.upsert_message(
                self.account,
                {
                    "gmail_message_id": gid,
                    "thread_id": gid,
                    "subject": "[GitHub] Sudo email verification code",
                    "sender": "GitHub",
                    "sender_email": "noreply@github.com",
                    "received_at": None,
                    "body_preview": "code",
                    "gmail_labels": [],
                    "truncated": False,
                    "unsupported_content": False,
                    "has_attachments": False,
                },
            )
            mid = self.account + ":" + gid
            self.db.insert_proposal(self.account, mid, labels, "r", "model", "m", "p", "l")
            self.db.save_decision(self.account, mid, "corrected", labels)
        self.db.close()
        self.app = app_module.create_app(self.config)
        self.client = self.app.test_client()
        self.csrf = self.app.config["CSRF_TOKEN"]

    def tearDown(self):
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_list_and_resolve_conflict(self):
        listed = self.client.get("/api/conflicts")
        self.assertEqual(listed.status_code, 200)
        groups = listed.get_json()["groups"]
        self.assertEqual(len(groups), 1)
        ids = [m["message_id"] for m in groups[0]["members"]]
        chosen = ["Type/Verification"]
        res = self.client.post(
            "/api/conflicts/resolve",
            json={"message_ids": ids, "label_ids": chosen},
            headers={"X-CSRF-Token": self.csrf, "Origin": "http://127.0.0.1:5000"},
        )
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        after = self.client.get("/api/conflicts").get_json()
        self.assertEqual(after["n_groups"], 0)

    def test_resolve_with_missing_message_is_atomic(self):
        """Finding 13: one bad id must not leave a partially-resolved group."""
        listed = self.client.get("/api/conflicts").get_json()
        ids = [m["message_id"] for m in listed["groups"][0]["members"]]
        res = self.client.post(
            "/api/conflicts/resolve",
            json={"message_ids": ids + ["missing:message"], "label_ids": ["Type/Verification"]},
            headers={"X-CSRF-Token": self.csrf, "Origin": "http://127.0.0.1:5000"},
        )
        self.assertEqual(res.status_code, 404)
        after = self.client.get("/api/conflicts").get_json()
        self.assertEqual(after["n_groups"], listed["n_groups"])


class TestSyncReview(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.temp_dir, "config.json")
        self.config = config_module.Config(self.config_path)
        self.db = db_module.DB(self.config.database_path)
        self.account = self.db.get_or_create_account("user@example.com")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _msg(self, gid, subject, labels):
        self.db.upsert_message(
            self.account,
            {
                "gmail_message_id": gid,
                "thread_id": gid,
                "subject": subject,
                "sender": "S",
                "sender_email": "s@e.com",
                "received_at": None,
                "body_preview": "body",
                "gmail_labels": [],
                "truncated": False,
                "unsupported_content": False,
                "has_attachments": False,
            },
        )
        mid = self.account + ":" + gid
        self.db.insert_proposal(self.account, mid, labels, "r", "model", "m", "p", "l")
        self.db.save_decision(self.account, mid, "corrected", labels)

    def test_conflicts_exit_2(self):
        from Mailroom import cli as cli_module

        self._msg("a", "Same subject here", ["Type/Receipt"])
        self._msg("b", "Same subject here", ["Type/Newsletter"])
        self.db.close()
        args = cli_module.create_cli_parser().parse_args(
            ["--config", self.config_path, "sync-review"]
        )
        code = cli_module.sync_review_cmd(args, self.config)
        self.assertEqual(code, 2)

    def test_no_conflicts_refreshes_examples(self):
        from Mailroom import cli as cli_module

        self._msg("a", "Unique grocery receipt XYZ", ["Type/Receipt"])
        self._msg("b", "Other unique invoice ABC", ["Type/Receipt"])
        self.db.close()
        args = cli_module.create_cli_parser().parse_args(
            ["--config", self.config_path, "sync-review"]
        )
        code = cli_module.sync_review_cmd(args, self.config)
        self.assertEqual(code, 0)
        cfg = config_module.Config(self.config_path)
        receipt = [l for l in cfg.labels if l["id"] == "Type/Receipt"][0]
        self.assertTrue(
            any("grocery" in (e or "").lower() or "invoice" in (e or "").lower() for e in receipt["examples"])
        )
