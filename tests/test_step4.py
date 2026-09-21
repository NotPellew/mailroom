"""Step 4 acceptance tests: review, decisions, export, and local safety.

These tests are synthetic; they do not require Gmail credentials or a model.
"""

import csv
import io
import json
import os
import shutil
import tempfile
import unittest

from EmailMan import app as app_module
from EmailMan import cli as cli_module
from EmailMan import config as config_module
from EmailMan import db as db_module
from EmailMan import export as export_module
from EmailMan import security as security_module
from EmailMan import routes as routes_module


class ReviewTestBase(unittest.TestCase):
    """Shared setup: isolated config, DB, account, and app test client."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.temp_dir, "config.json")
        self.config = config_module.Config(self.config_path)
        self.db = db_module.DB(self.config.database_path)
        self.account_id = self.db.get_or_create_account("user@example.com")
        self.db.close()

        self.app = app_module.create_app(self.config)
        self.client = self.app.test_client()
        self.csrf = self.app.config["CSRF_TOKEN"]

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def add_message(self, gmail_id, subject="Subject", body="Hello", sender="Sender"):
        db = db_module.DB(self.config.database_path)
        try:
            db.upsert_message(
                self.account_id,
                {
                    "gmail_message_id": gmail_id,
                    "thread_id": "th-" + gmail_id,
                    "subject": subject,
                    "sender": sender,
                    "sender_email": "sender@example.com",
                    "received_at": "2026-01-01T00:00:00",
                    "body_preview": body,
                    "gmail_labels": [],
                    "truncated": False,
                    "unsupported_content": False,
                    "has_attachments": False,
                },
            )
        finally:
            db.close()
        return f"{self.account_id}:{gmail_id}"

    def add_proposal(self, message_id, label_ids, reason="r", abstain=False):
        db = db_module.DB(self.config.database_path)
        try:
            return db.insert_proposal(
                self.account_id,
                message_id,
                label_ids,
                reason,
                "model",
                "m1",
                "pv",
                "lv",
                abstain=abstain,
            )
        finally:
            db.close()

    def post_decision(self, payload, origin="http://127.0.0.1:5000", csrf=True):
        headers = {}
        if origin is not None:
            headers["Origin"] = origin
        if csrf:
            headers["X-CSRF-Token"] = self.csrf
        return self.client.post("/api/decisions", json=payload, headers=headers)


class TestDecisionPersistence(ReviewTestBase):
    def test_save_and_get_decision(self):
        mid = self.add_message("m1")
        db = db_module.DB(self.config.database_path)
        try:
            db.save_decision(self.account_id, mid, "accepted", ["Type/Receipt"], "ok")
            decision = db.get_decision(mid)
            self.assertEqual(decision["status"], "accepted")
            self.assertEqual(decision["label_ids_parsed"], ["Type/Receipt"])
            self.assertEqual(decision["user_notes"], "ok")
        finally:
            db.close()

    def test_resave_updates_single_row(self):
        mid = self.add_message("m1")
        db = db_module.DB(self.config.database_path)
        try:
            first = db.save_decision(self.account_id, mid, "accepted", ["Type/Receipt"])
            second = db.save_decision(self.account_id, mid, "corrected", ["Type/Newsletter"])
            self.assertEqual(first, second)
            count = db.conn.execute(
                "SELECT COUNT(*) AS c FROM decisions WHERE message_id = ?", (mid,)
            ).fetchone()["c"]
            self.assertEqual(count, 1)
            self.assertEqual(db.get_decision(mid)["status"], "corrected")
        finally:
            db.close()

    def test_empty_selection_distinct_from_skip(self):
        mid1 = self.add_message("m1")
        mid2 = self.add_message("m2")
        db = db_module.DB(self.config.database_path)
        try:
            db.save_decision(self.account_id, mid1, "corrected", [])
            db.save_decision(self.account_id, mid2, "skipped", [])
            self.assertEqual(db.get_decision(mid1)["status"], "corrected")
            self.assertEqual(db.get_decision(mid1)["label_ids_parsed"], [])
            self.assertEqual(db.get_decision(mid2)["status"], "skipped")
            self.assertEqual(db.get_decision(mid2)["label_ids_parsed"], [])
        finally:
            db.close()

    def test_skip_cannot_carry_labels(self):
        mid = self.add_message("m1")
        db = db_module.DB(self.config.database_path)
        try:
            with self.assertRaises(db_module.DatabaseError):
                db.save_decision(self.account_id, mid, "skipped", ["Type/Receipt"])
            with self.assertRaises(db_module.DatabaseError):
                db.save_decision(self.account_id, mid, "bogus", [])
            with self.assertRaises(db_module.DatabaseError):
                db.save_decision(self.account_id, "missing:msg", "skipped", [])
        finally:
            db.close()

    def test_decision_survives_restart(self):
        mid = self.add_message("m1")
        db = db_module.DB(self.config.database_path)
        db.save_decision(self.account_id, mid, "accepted", ["Type/Receipt"])
        db.close()

        db2 = db_module.DB(self.config.database_path)
        try:
            self.assertEqual(db2.get_decision(mid)["status"], "accepted")
        finally:
            db2.close()


class TestReviewListing(ReviewTestBase):
    def test_latest_proposal_is_used(self):
        mid = self.add_message("m1")
        self.add_proposal(mid, ["Type/Receipt"], reason="old")
        self.add_proposal(mid, ["Type/Newsletter"], reason="new")

        db = db_module.DB(self.config.database_path)
        try:
            item = db.get_review_item(mid)
        finally:
            db.close()
        self.assertEqual(item["proposal_reason"], "new")
        self.assertEqual(item["proposal_label_ids"], ["Type/Newsletter"])
        self.assertEqual(item["review_status"], "unreviewed")

    def test_reclassification_preserves_decision_and_flags_newer(self):
        mid = self.add_message("m1")
        self.add_proposal(mid, ["Type/Receipt"], reason="first")
        db = db_module.DB(self.config.database_path)
        try:
            db.save_decision(self.account_id, mid, "accepted", ["Type/Receipt"])
        finally:
            db.close()
        # A later proposal must not overwrite the human choice.
        self.add_proposal(mid, ["Type/Newsletter"], reason="second")

        db = db_module.DB(self.config.database_path)
        try:
            item = db.get_review_item(mid)
            decision = db.get_decision(mid)
        finally:
            db.close()
        self.assertEqual(decision["status"], "accepted")
        self.assertEqual(decision["label_ids_parsed"], ["Type/Receipt"])
        self.assertEqual(item["proposal_label_ids"], ["Type/Newsletter"])

        payload = routes_module._review_item_payload(item)
        self.assertTrue(payload["newer_proposal_than_decision"])
        self.assertEqual(payload["decision"]["label_ids"], ["Type/Receipt"])

    def test_filters_by_state_label_and_status(self):
        proposed = self.add_message("m-proposed")
        uncertain = self.add_message("m-uncertain")
        error = self.add_message("m-error")
        self.add_proposal(proposed, ["Type/Receipt"], reason="r")
        self.add_proposal(uncertain, [], reason="not sure", abstain=True)

        db = db_module.DB(self.config.database_path)
        try:
            db.save_decision(self.account_id, proposed, "accepted", ["Type/Receipt"])
            all_items = db.list_review_items()
            proposed_items = db.list_review_items(state="proposed")
            uncertain_items = db.list_review_items(state="uncertain")
            error_items = db.list_review_items(state="error")
            accepted_items = db.list_review_items(status="accepted")
            label_items = db.list_review_items(label="Type/Receipt")
        finally:
            db.close()

        self.assertEqual(len(all_items), 3)
        self.assertEqual([i["message_id"] for i in proposed_items], [proposed])
        self.assertEqual([i["message_id"] for i in uncertain_items], [uncertain])
        self.assertEqual([i["message_id"] for i in error_items], [error])
        self.assertEqual([i["message_id"] for i in accepted_items], [proposed])
        self.assertEqual([i["message_id"] for i in label_items], [proposed])

    def test_disagreement_filter_compares_kind_axis(self):
        agree = self.add_message("m-agree")
        differ = self.add_message("m-differ")
        overlay = self.add_message("m-overlay")
        skipped = self.add_message("m-skipped")

        self.add_proposal(agree, ["Type/Targeted"])
        self.add_proposal(differ, ["Type/Newsletter"])
        self.add_proposal(overlay, ["Type/Targeted", "Type/NeedsReply", "Type/NeedsAction"])
        self.add_proposal(skipped, ["Type/Newsletter"])

        db = db_module.DB(self.config.database_path)
        try:
            db.save_decision(self.account_id, agree, "accepted", ["Type/Targeted"])
            db.save_decision(self.account_id, differ, "corrected", ["Type/Targeted"])
            db.save_decision(
                self.account_id,
                overlay,
                "accepted",
                ["Type/Targeted", "Type/NeedsReply", "Type/NeedsAction"],
            )
            db.save_decision(self.account_id, skipped, "skipped", [])
            items = db.list_review_items(disagreement=True)
            total = db.count_review_items(disagreement=True)
        finally:
            db.close()

        # NeedsReply/NeedsAction are overlays (ignored); skipped is not a saved kind choice.
        self.assertEqual([i["message_id"] for i in items], [differ])
        self.assertEqual(total, 1)

        data = self.client.get("/api/review?disagrees=1").get_json()
        self.assertEqual(data["total"], 1)
        self.assertEqual([i["message_id"] for i in data["items"]], [differ])

    def test_api_review_list_and_item(self):
        mid = self.add_message("m1", subject="Hi", body="<script>alert(1)</script>")
        self.add_proposal(mid, ["Type/Receipt"], reason="looks like a receipt")

        listing = self.client.get("/api/review")
        self.assertEqual(listing.status_code, 200)
        body = listing.get_json()
        self.assertEqual(body["count"], 1)
        item = body["items"][0]
        self.assertEqual(item["subject"], "Hi")
        self.assertNotIn("body_preview", item)
        self.assertEqual(item["proposal"]["label_ids"], ["Type/Receipt"])
        self.assertEqual(item["suggestion_state"], "proposed")
        self.assertEqual(item["review_status"], "unreviewed")
        self.assertEqual(
            item["gmail_url"],
            "https://mail.google.com/mail/u/0/?authuser=user%40example.com#all/m1",
        )

        detail = self.client.get("/api/review/item?message_id=" + mid)
        self.assertEqual(detail.status_code, 200)
        # The script tag is returned as an inert string, not markup.
        self.assertEqual(detail.get_json()["body_preview"], "<script>alert(1)</script>")

        missing = self.client.get("/api/review/item?message_id=nope")
        self.assertEqual(missing.status_code, 404)

    def test_label_filter_tolerates_corrupt_proposal_json(self):
        mid = self.add_message("m1")
        self.add_proposal(mid, ["Type/Receipt"])
        db = db_module.DB(self.config.database_path)
        try:
            db.conn.execute(
                "UPDATE proposals SET label_ids = ? WHERE message_id = ?", ("not json", mid)
            )
            db.conn.commit()
        finally:
            db.close()

        # A malformed stored value must not 500 the proposed-label filter.
        res = self.client.get("/api/review?label=Type/Receipt")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["total"], 0)
        # Without the filter the row is still listed (labels parse to []).
        self.assertEqual(self.client.get("/api/review").get_json()["total"], 1)


class TestApiDecisions(ReviewTestBase):
    def test_accept_uses_proposal_labels(self):
        mid = self.add_message("m1")
        self.add_proposal(mid, ["Type/Receipt"])
        res = self.post_decision({"message_id": mid, "status": "accepted"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["label_ids"], ["Type/Receipt"])

    def test_reclassification_after_api_acceptance_flags_newer(self):
        mid = self.add_message("m1")
        self.add_proposal(mid, ["Type/Receipt"])
        self.assertEqual(
            self.post_decision({"message_id": mid, "status": "accepted"}).status_code, 200
        )
        # A rerun produces a new proposal; the saved choice is untouched.
        self.add_proposal(mid, ["Type/Newsletter"])
        detail = self.client.get("/api/review/item?message_id=" + mid).get_json()
        self.assertEqual(detail["decision"]["label_ids"], ["Type/Receipt"])
        self.assertEqual(detail["proposal"]["label_ids"], ["Type/Newsletter"])
        self.assertTrue(detail["newer_proposal_than_decision"])

    def test_accept_refused_for_abstained_proposal(self):
        mid = self.add_message("m1")
        self.add_proposal(mid, [], reason="unsure", abstain=True)
        res = self.post_decision({"message_id": mid, "status": "accepted"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("abstain", res.get_json()["error"].lower())

        # Manual correction remains available.
        corrected = self.post_decision(
            {"message_id": mid, "status": "corrected", "label_ids": ["Type/Newsletter"]}
        )
        self.assertEqual(corrected.status_code, 200)

    def test_accept_refused_without_proposal(self):
        mid = self.add_message("m1")
        res = self.post_decision({"message_id": mid, "status": "accepted"})
        self.assertEqual(res.status_code, 400)

    def test_accept_refused_when_proposal_labels_no_longer_configured(self):
        mid = self.add_message("m1")
        self.add_proposal(mid, ["No/LongerConfigured"])
        res = self.post_decision({"message_id": mid, "status": "accepted"})
        self.assertEqual(res.status_code, 400)
        # Correction is still possible.
        self.assertEqual(
            self.post_decision(
                {"message_id": mid, "status": "corrected", "label_ids": ["Type/Receipt"]}
            ).status_code,
            200,
        )

    def test_correct_with_empty_selection(self):
        mid = self.add_message("m1")
        self.add_proposal(mid, ["Type/Receipt"])
        res = self.post_decision({"message_id": mid, "status": "corrected", "label_ids": []})
        self.assertEqual(res.status_code, 200)
        db = db_module.DB(self.config.database_path)
        try:
            self.assertEqual(db.get_decision(mid)["status"], "corrected")
            self.assertEqual(db.get_decision(mid)["label_ids_parsed"], [])
        finally:
            db.close()

    def test_skip_and_reopen(self):
        mid = self.add_message("m1")
        self.add_proposal(mid, ["Type/Receipt"])
        self.assertEqual(self.post_decision({"message_id": mid, "status": "skipped"}).status_code, 200)
        self.assertEqual(self.post_decision({"message_id": mid, "status": "pending"}).status_code, 200)
        db = db_module.DB(self.config.database_path)
        try:
            decision = db.get_decision(mid)
            self.assertEqual(decision["status"], "pending")
            self.assertEqual(decision["label_ids_parsed"], [])
        finally:
            db.close()

    def test_rejects_unknown_label_and_bad_status(self):
        mid = self.add_message("m1")
        self.add_proposal(mid, ["Type/Receipt"])
        bad_label = self.post_decision(
            {"message_id": mid, "status": "corrected", "label_ids": ["Not/A/Label"]}
        )
        self.assertEqual(bad_label.status_code, 400)
        bad_status = self.post_decision({"message_id": mid, "status": "maybe"})
        self.assertEqual(bad_status.status_code, 400)
        missing = self.post_decision({"message_id": "gone", "status": "skipped"})
        self.assertEqual(missing.status_code, 404)

    def test_correction_keeps_previously_saved_retired_label(self):
        mid = self.add_message("m1")
        db = db_module.DB(self.config.database_path)
        try:
            db.save_decision(self.account_id, mid, "corrected", ["Gone/Label"], validate_labels=False)
        finally:
            db.close()
        res = self.post_decision(
            {
                "message_id": mid,
                "status": "corrected",
                "label_ids": ["Gone/Label", "Type/Receipt"],
            }
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["label_ids"], ["Gone/Label", "Type/Receipt"])
        # The value must actually be persisted, not just echoed back.
        db = db_module.DB(self.config.database_path)
        try:
            stored = db.get_decision(mid)
        finally:
            db.close()
        self.assertEqual(stored["label_ids_parsed"], ["Gone/Label", "Type/Receipt"])

    def test_correction_can_drop_retired_label(self):
        mid = self.add_message("m1")
        db = db_module.DB(self.config.database_path)
        try:
            db.save_decision(self.account_id, mid, "corrected", ["Gone/Label", "Type/Receipt"], validate_labels=False)
        finally:
            db.close()
        res = self.post_decision(
            {"message_id": mid, "status": "corrected", "label_ids": ["Type/Receipt"]}
        )
        self.assertEqual(res.status_code, 200)
        db = db_module.DB(self.config.database_path)
        try:
            self.assertEqual(db.get_decision(mid)["label_ids_parsed"], ["Type/Receipt"])
        finally:
            db.close()

    def test_accept_replaces_retired_saved_label(self):
        mid = self.add_message("m1")
        db = db_module.DB(self.config.database_path)
        try:
            db.save_decision(self.account_id, mid, "corrected", ["Gone/Label"], validate_labels=False)
        finally:
            db.close()
        self.add_proposal(mid, ["Type/Receipt"])
        res = self.post_decision({"message_id": mid, "status": "accepted"})
        self.assertEqual(res.status_code, 200)
        db = db_module.DB(self.config.database_path)
        try:
            self.assertEqual(db.get_decision(mid)["label_ids_parsed"], ["Type/Receipt"])
        finally:
            db.close()

    def test_correction_keeps_retired_label_from_current_proposal(self):
        mid = self.add_message("m1")
        self.add_proposal(mid, ["Gone/Label"])
        res = self.post_decision(
            {"message_id": mid, "status": "corrected", "label_ids": ["Gone/Label"]}
        )
        self.assertEqual(res.status_code, 200)

    def test_correction_rejects_brand_new_unknown_label(self):
        mid = self.add_message("m1")
        res = self.post_decision(
            {"message_id": mid, "status": "corrected", "label_ids": ["Totally/New"]}
        )
        self.assertEqual(res.status_code, 400)

    def test_user_notes_must_be_string(self):
        mid = self.add_message("m1")
        res = self.post_decision(
            {"message_id": mid, "status": "corrected", "label_ids": [], "user_notes": {"x": 1}}
        )
        self.assertEqual(res.status_code, 400)


class TestLocalRequestSafety(ReviewTestBase):
    def test_cross_origin_post_rejected(self):
        mid = self.add_message("m1")
        res = self.post_decision(
            {"message_id": mid, "status": "skipped"}, origin="http://evil.example"
        )
        self.assertEqual(res.status_code, 403)

    def test_missing_csrf_token_rejected_for_browser_request(self):
        mid = self.add_message("m1")
        res = self.post_decision({"message_id": mid, "status": "skipped"}, csrf=False)
        self.assertEqual(res.status_code, 403)

    def test_valid_browser_request_allowed(self):
        mid = self.add_message("m1")
        res = self.post_decision({"message_id": mid, "status": "skipped"})
        self.assertEqual(res.status_code, 200)

    def test_tokenless_mutation_is_rejected_even_without_origin(self):
        # Every mutation requires the token, not only browser requests.
        mid = self.add_message("m1")
        res = self.client.post(
            "/api/decisions", json={"message_id": mid, "status": "skipped"}
        )
        self.assertEqual(res.status_code, 403)

    def test_tokenless_classify_is_rejected(self):
        res = self.client.post("/api/classify", json={"message_id": "user@example.com:gm-1"})
        self.assertEqual(res.status_code, 403)

    def test_unexpected_host_header_rejected(self):
        res = self.client.get("/api/status", headers={"Host": "evil.example"})
        self.assertEqual(res.status_code, 400)

    def test_csrf_endpoint_matches_app_token(self):
        res = self.client.get("/api/csrf")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["csrf_token"], self.csrf)

    def test_security_headers_present(self):
        res = self.client.get("/")
        self.assertEqual(res.headers.get("X-Frame-Options"), "DENY")
        self.assertIn("frame-ancestors 'none'", res.headers.get("Content-Security-Policy", ""))
        self.assertEqual(res.headers.get("X-Content-Type-Options"), "nosniff")

    def test_origin_null_rejected(self):
        mid = self.add_message("m1")
        res = self.post_decision({"message_id": mid, "status": "skipped"}, origin="null")
        self.assertEqual(res.status_code, 403)

    def test_evil_referer_without_origin_rejected(self):
        mid = self.add_message("m1")
        res = self.client.post(
            "/api/decisions",
            json={"message_id": mid, "status": "skipped"},
            headers={"Referer": "http://evil.example/page", "X-CSRF-Token": self.csrf},
        )
        self.assertEqual(res.status_code, 403)

    def test_loopback_referer_without_origin_allowed(self):
        mid = self.add_message("m1")
        res = self.client.post(
            "/api/decisions",
            json={"message_id": mid, "status": "skipped"},
            headers={"Referer": "http://127.0.0.1:5000/", "X-CSRF-Token": self.csrf},
        )
        self.assertEqual(res.status_code, 200)

    def test_form_post_cannot_mutate(self):
        mid = self.add_message("m1")
        # Valid token but form encoding: no JSON body is parsed, so no mutation.
        res = self.client.post(
            "/api/decisions",
            data={"message_id": mid, "status": "skipped"},
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(res.status_code, 400)
        db = db_module.DB(self.config.database_path)
        try:
            self.assertIsNone(db.get_decision(mid))
        finally:
            db.close()

    def test_security_helpers(self):
        self.assertTrue(security_module.is_allowed_host_header("127.0.0.1:5000"))
        self.assertTrue(security_module.is_allowed_host_header("localhost"))
        self.assertTrue(security_module.is_allowed_host_header("[::1]:5000"))
        self.assertFalse(security_module.is_allowed_host_header("evil.example"))
        self.assertTrue(security_module.is_allowed_origin_or_referer("http://127.0.0.1:5000"))
        self.assertFalse(security_module.is_allowed_origin_or_referer("https://evil.example"))
        self.assertFalse(security_module.csrf_token_matches("a", "b"))
        self.assertTrue(security_module.csrf_token_matches("a", "a"))


class TestEndpointOverride(ReviewTestBase):
    def test_api_rejects_non_loopback_override(self):
        res = self.client.post(
            "/api/classify",
            json={
                "message_id": "user@example.com:gm-1",
                "model_endpoint": "http://evil.example/v1",
            },
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("loopback", res.get_json()["error"].lower())

    def test_api_accepts_loopback_override_past_validation(self):
        # Passes override validation, then fails on the missing message.
        res = self.client.post(
            "/api/classify",
            json={
                "message_id": "user@example.com:gm-1",
                "model_endpoint": "http://127.0.0.1:8080/completion",
            },
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(res.status_code, 404)

    def test_cli_rejects_non_loopback_override(self):
        args = cli_module.create_cli_parser().parse_args(
            [
                "--config",
                self.config_path,
                "classify",
                "--message-id",
                "user@example.com:gm-1",
                "--model-endpoint",
                "http://evil.example/v1",
            ]
        )
        self.assertEqual(cli_module.classify_cmd(args, self.config), 1)

    def test_config_helper(self):
        from EmailMan.config import is_loopback_url

        self.assertTrue(is_loopback_url("http://127.0.0.1:8080/completion"))
        self.assertTrue(is_loopback_url("http://localhost:8080"))
        self.assertFalse(is_loopback_url("http://evil.example/v1"))
        self.assertFalse(is_loopback_url("ftp://127.0.0.1"))
        self.assertFalse(is_loopback_url(""))


class TestReviewPagination(ReviewTestBase):
    def test_limit_offset_and_total(self):
        for i in range(5):
            self.add_message("m%d" % i, subject="s%d" % i)

        first = self.client.get("/api/review?limit=2&offset=0").get_json()
        self.assertEqual(first["total"], 5)
        self.assertEqual(first["count"], 2)
        self.assertEqual(first["limit"], 2)
        self.assertEqual(first["offset"], 0)

        second = self.client.get("/api/review?limit=2&offset=2").get_json()
        self.assertEqual(second["count"], 2)
        last = self.client.get("/api/review?limit=2&offset=4").get_json()
        self.assertEqual(last["count"], 1)

        all_ids = [i["message_id"] for i in first["items"] + second["items"] + last["items"]]
        self.assertEqual(len(set(all_ids)), 5)

    def test_limit_is_clamped_to_max(self):
        self.add_message("m1")
        data = self.client.get("/api/review?limit=100000").get_json()
        self.assertEqual(data["limit"], 200)

    def test_total_respects_filters(self):
        self.add_message("m-proposed")
        uncertain = self.add_message("m-uncertain")
        self.add_proposal("user@example.com:m-proposed", ["Type/Receipt"])
        self.add_proposal(uncertain, [], abstain=True)
        data = self.client.get("/api/review?state=uncertain").get_json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["count"], 1)

    def test_list_omits_body_but_detail_includes_it(self):
        mid = self.add_message("m1", body="top secret body")
        db = db_module.DB(self.config.database_path)
        try:
            listed = db.list_review_items(message_id=mid)[0]
            detailed = db.get_review_item(mid)
        finally:
            db.close()
        self.assertIsNone(listed["body_preview"])
        self.assertTrue(listed["has_body"])
        self.assertEqual(detailed["body_preview"], "top secret body")


class TestExport(ReviewTestBase):
    def _seed(self):
        accepted = self.add_message("m-accepted")
        corrected = self.add_message("m-corrected")
        skipped = self.add_message("m-skipped")
        db = db_module.DB(self.config.database_path)
        try:
            db.save_decision(self.account_id, accepted, "accepted", ["Type/Receipt"])
            db.save_decision(self.account_id, corrected, "corrected", ["Type/Newsletter"])
            db.save_decision(self.account_id, skipped, "skipped", [])
        finally:
            db.close()
        return accepted, corrected, skipped

    def test_csv_default_excludes_skipped(self):
        accepted, corrected, _ = self._seed()
        res = self.client.get("/api/export?format=csv")
        self.assertEqual(res.status_code, 200)
        text = res.get_data(as_text=True)
        rows = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual(len(rows), 2)
        by_status = {row["status"]: row for row in rows}
        self.assertEqual(set(by_status), {"accepted", "corrected"})
        # Values match the saved selections, not just field presence.
        self.assertEqual(by_status["accepted"]["account_id"], self.account_id)
        self.assertEqual(by_status["accepted"]["message_id"], "m-accepted")
        self.assertEqual(by_status["accepted"]["thread_id"], "th-m-accepted")
        self.assertEqual(by_status["accepted"]["label_ids"], "Type/Receipt")
        self.assertEqual(by_status["accepted"]["label_names"], "Receipt")
        self.assertEqual(by_status["corrected"]["label_ids"], "Type/Newsletter")
        self.assertEqual(by_status["corrected"]["label_names"], "Newsletter")
        # No message body or model reason leaks into the export.
        self.assertNotIn("Hello", text)
        self.assertNotIn("looks like", text)
        for field in export_module.EXPORT_FIELDS:
            self.assertIn(field, rows[0])

    def test_csv_include_skipped(self):
        self._seed()
        res = self.client.get("/api/export?format=csv&include_skipped=1")
        rows = list(csv.DictReader(io.StringIO(res.get_data(as_text=True))))
        self.assertEqual(len(rows), 3)
        self.assertIn("skipped", {row["status"] for row in rows})

    def test_json_export(self):
        self._seed()
        res = self.client.get("/api/export?format=json")
        self.assertEqual(res.status_code, 200)
        payload = json.loads(res.get_data(as_text=True))
        self.assertEqual(payload["count"], 2)
        by_status = {d["status"]: d for d in payload["decisions"]}
        self.assertEqual(set(by_status), {"accepted", "corrected"})
        self.assertEqual(by_status["accepted"]["label_ids"], ["Type/Receipt"])
        self.assertEqual(by_status["accepted"]["label_names"], ["Receipt"])
        self.assertEqual(by_status["accepted"]["message_id"], "m-accepted")
        self.assertNotIn("body_preview", by_status["accepted"])
        self.assertNotIn("reason", by_status["accepted"])
        for field in export_module.EXPORT_FIELDS:
            self.assertIn(field, payload["decisions"][0])

    def test_api_export_neutralizes_formula_label(self):
        # A stored label id that is a spreadsheet formula must be neutralized
        # through the real /api/export path, not just the helper.
        mid = self.add_message("m-formula")
        db = db_module.DB(self.config.database_path)
        try:
            db.save_decision(self.account_id, mid, "corrected", ["=cmd|' /C calc'!A0"], validate_labels=False)
        finally:
            db.close()
        res = self.client.get("/api/export?format=csv")
        text = res.get_data(as_text=True)
        self.assertIn("'=cmd", text)
        self.assertNotIn("\n=cmd", text)

    def test_formula_cells_neutralized_in_csv(self):
        record = {
            "account_id": "acct",
            "message_id": "gm",
            "thread_id": "th",
            "label_ids": ["=cmd"],
            "label_names": ["+payload"],
            "status": "corrected",
            "reviewed_at": "@now",
        }
        text = export_module.records_to_csv([record])
        self.assertIn("'=cmd", text)
        self.assertIn("'+payload", text)
        self.assertIn("'@now", text)

    def test_formula_neutralization_edge_cases(self):
        neutralize = export_module.neutralize_csv_cell
        for dangerous in ("=1+1", "+cmd", "-2+2", "@cmd", "\t=cmd", "\r=cmd"):
            self.assertTrue(neutralize(dangerous).startswith("'"), dangerous)
        for indented in (" =1+1", "  @cmd", "\u00a0=1+1", "\n+cmd"):
            self.assertTrue(neutralize(indented).startswith("'"), repr(indented))
        for safe in ("", "Type/Receipt", "hello", "1+1", "a=b", "Δelta"):
            self.assertEqual(neutralize(safe), safe)
        # A leading UTF-8 BOM is stripped before neutralization.
        self.assertEqual(neutralize("\ufeff=1+1"), "'=1+1")
        self.assertEqual(neutralize("\ufeffsafe"), "safe")
        self.assertEqual(neutralize("\ufeff\ufeff=1+1"), "'=1+1")
        # Unicode spaces, fullwidth triggers, and legacy DDE `|`.
        for lookalike in ("＝1+1", "＋cmd", "－2+2", "＠SUM", "|cmd", "\u2000=1+1", "\u3000@cmd"):
            self.assertTrue(neutralize(lookalike).startswith("'"), repr(lookalike))
        # Quotes and embedded newlines are left for the CSV writer to escape.
        self.assertEqual(neutralize('say "hi"'), 'say "hi"')

    def test_cli_export_writes_file(self):
        self._seed()
        out = os.path.join(self.temp_dir, "out.csv")
        args = cli_module.create_cli_parser().parse_args(
            ["--config", self.config_path, "export", "--format", "csv", "--output", out]
        )
        code = cli_module.export_cmd(args, self.config)
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(out))
        with open(out, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 2)

    def test_export_rejects_unknown_format(self):
        res = self.client.get("/api/export?format=xml")
        self.assertEqual(res.status_code, 400)


class TestV3ToV4Migration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "EmailMan.db")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_v3_db(self, proposal_label_sets, decision_label_ids):
        import sqlite3

        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
            INSERT INTO schema_version (version) VALUES (3);
            CREATE TABLE accounts (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL,
                gmail_id TEXT UNIQUE, created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_sync TIMESTAMP, last_query TEXT, gmail_label_catalog TEXT,
                is_active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE messages (
                id TEXT PRIMARY KEY, thread_id TEXT, account_id TEXT NOT NULL,
                gmail_message_id TEXT NOT NULL, subject TEXT, sender TEXT,
                sender_email TEXT, received_at TIMESTAMP, body_preview TEXT,
                gmail_labels TEXT, fetched_at TIMESTAMP,
                truncated INTEGER NOT NULL DEFAULT 0,
                unsupported_content INTEGER NOT NULL DEFAULT 0,
                has_attachments INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE proposals (
                id TEXT PRIMARY KEY, message_id TEXT NOT NULL, account_id TEXT NOT NULL,
                label_ids TEXT NOT NULL, label_names TEXT, reason TEXT, confidence REAL,
                source TEXT NOT NULL, model_version TEXT NOT NULL,
                prompt_version TEXT NOT NULL, label_definition_version TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE decisions (
                id TEXT PRIMARY KEY, message_id TEXT NOT NULL, account_id TEXT NOT NULL,
                status TEXT NOT NULL, label_ids TEXT NOT NULL, user_notes TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.execute("INSERT INTO accounts (id, name, email) VALUES ('a@e.com', 'A', 'a@e.com')")
        conn.execute(
            "INSERT INTO messages (id, account_id, gmail_message_id) "
            "VALUES ('a@e.com:m1', 'a@e.com', 'm1')"
        )
        for index, labels in enumerate(proposal_label_sets):
            conn.execute(
                "INSERT INTO proposals (id, message_id, account_id, label_ids, source, "
                "model_version, prompt_version, label_definition_version) "
                "VALUES (?, 'a@e.com:m1', 'a@e.com', ?, 'model', 'm', 'pv', 'lv')",
                (f"p{index + 1}", json.dumps(labels)),
            )
        conn.execute(
            "INSERT INTO decisions (id, message_id, account_id, status, label_ids) "
            "VALUES ('d1', 'a@e.com:m1', 'a@e.com', 'accepted', ?)",
            (json.dumps(decision_label_ids),),
        )
        conn.commit()
        conn.close()

    def test_v3_to_v4_adds_abstain_and_decision_proposal(self):
        self._create_v3_db([["Type/Receipt"]], ["Type/Receipt"])

        db_obj = db_module.DB(self.db_path)
        try:
            proposal_cols = {
                row["name"] for row in db_obj.conn.execute("PRAGMA table_info(proposals)").fetchall()
            }
            decision_cols = {
                row["name"] for row in db_obj.conn.execute("PRAGMA table_info(decisions)").fetchall()
            }
            self.assertIn("abstain", proposal_cols)
            self.assertIn("proposal_id", decision_cols)
            version = db_obj.conn.execute(
                "SELECT MAX(version) AS v FROM schema_version"
            ).fetchone()["v"]
            self.assertEqual(version, db_obj.SCHEMA_VERSION)

            # Legacy decision is backfilled to its (only) proposal and is not
            # falsely reported as stale.
            backfilled = db_obj.conn.execute(
                "SELECT proposal_id FROM decisions WHERE id = 'd1'"
            ).fetchone()["proposal_id"]
            self.assertEqual(backfilled, "p1")
            item = db_obj.get_review_item("a@e.com:m1")
            payload = routes_module._review_item_payload(item)
            self.assertFalse(payload["newer_proposal_than_decision"])
            self.assertEqual(payload["decision"]["label_ids"], ["Type/Receipt"])
        finally:
            db_obj.close()

    def test_v3_multi_proposal_decision_is_not_backfilled(self):
        # With more than one proposal the decision basis is unknowable, so it
        # must stay NULL and the UI must flag the possible mismatch.
        self._create_v3_db(
            [["Type/Receipt"], ["Type/Newsletter"]], ["Type/Receipt"]
        )

        db_obj = db_module.DB(self.db_path)
        try:
            backfilled = db_obj.conn.execute(
                "SELECT proposal_id FROM decisions WHERE id = 'd1'"
            ).fetchone()["proposal_id"]
            self.assertIsNone(backfilled)
            item = db_obj.get_review_item("a@e.com:m1")
            payload = routes_module._review_item_payload(item)
            self.assertTrue(payload["newer_proposal_than_decision"])
        finally:
            db_obj.close()


class TestInertRendering(unittest.TestCase):
    def test_template_uses_text_not_innerhtml_and_has_no_remote_loads(self):
        template_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "EmailMan", "templates", "index.html"
        )
        with open(template_path, encoding="utf-8") as f:
            html = f.read()

        # No HTML-injection sinks are used with untrusted data.
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(sink, html, sink)
        # Body and other untrusted fields are inserted as text.
        self.assertIn("text: item.body_preview", html)
        self.assertIn("text: item.subject", html)
        self.assertIn("text: item.proposal.reason", html)
        # Saved labels that are no longer configured stay visible/selected,
        # are visually marked, and prompt before being dropped.
        self.assertIn("no longer configured", html)
        self.assertIn("label-choice stale", html)
        self.assertIn("droppedStaleLabels", html)
        # Gmail links are only set when the URL has the expected prefix.
        self.assertIn('indexOf("https://mail.google.com/")', html)
        # No remote scripts/images are loaded by the review page.
        self.assertNotIn("src=\"http", html)
        self.assertNotIn("src='http", html)
        self.assertNotIn("<link", html)

    def test_review_item_body_is_returned_as_data(self):
        temp_dir = tempfile.mkdtemp()
        try:
            cfg = config_module.Config(os.path.join(temp_dir, "config.json"))
            db = db_module.DB(cfg.database_path)
            account = db.get_or_create_account("user@example.com")
            payload = '<img src="http://evil.example/x.png"><script>bad()</script>'
            db.upsert_message(
                account,
                {
                    "gmail_message_id": "gm-x",
                    "thread_id": "th-x",
                    "subject": "<b>subject</b>",
                    "sender": "S",
                    "sender_email": "s@example.com",
                    "received_at": None,
                    "body_preview": payload,
                    "gmail_labels": [],
                    "truncated": False,
                    "unsupported_content": False,
                    "has_attachments": False,
                },
            )
            db.close()

            app = app_module.create_app(cfg)
            client = app.test_client()
            res = client.get("/api/review/item?message_id=" + account + ":gm-x")
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            # The untrusted markup is carried verbatim as data for the inert
            # text renderer; it is never executed or used as markup server-side.
            self.assertEqual(data["body_preview"], payload)
            self.assertEqual(data["subject"], "<b>subject</b>")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
