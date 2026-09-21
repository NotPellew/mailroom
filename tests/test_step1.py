"""Comprehensive test suite for Step 1: App skeleton and configuration."""

import unittest
import os
import json
import sqlite3
import tempfile
import email
from email import policy
from pathlib import Path
from unittest.mock import MagicMock, patch

from EmailMan import db, config as config_module, app as app_module, cli as cli_module, routes as routes_module


class TestAppSkeleton(unittest.TestCase):
    """Test the app skeleton, configuration, database, and review shell."""

    def setUp(self):
        """Set up isolated test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.temp_dir, "config.json")
        self.db_path = os.path.join(self.temp_dir, "EmailMan.db")

    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_package_layout(self):
        """Test that required package files, config example, templates, and fixtures exist."""
        from EmailMan import config, db, app, cli, routes

        self.assertTrue(hasattr(config, "Config"))
        self.assertTrue(hasattr(config, "ConfigError"))
        self.assertTrue(hasattr(config, "is_loopback_host"))

        self.assertTrue(hasattr(db, "DB"))
        self.assertTrue(hasattr(db, "DatabaseError"))

        self.assertTrue(hasattr(app, "create_app"))
        self.assertTrue(hasattr(app, "run_review_server"))

        self.assertTrue(hasattr(cli, "create_cli_parser"))
        self.assertTrue(hasattr(routes, "bp"))
        self.assertTrue(hasattr(routes, "get_status"))

        repo_root = Path(__file__).parent.parent
        self.assertTrue((repo_root / "config.example.json").exists(), "config.example.json missing")

        templates_dir = repo_root / "EmailMan" / "templates"
        self.assertTrue(templates_dir.exists())
        self.assertTrue((templates_dir / "index.html").exists())

        fixtures_dir = repo_root / "EmailMan" / "fixtures"
        self.assertTrue(fixtures_dir.exists())
        self.assertTrue((fixtures_dir / "mail_1.eml").exists())
        self.assertTrue((fixtures_dir / "mail_2.eml").exists())

    def test_database_init_and_schema(self):
        """Test database initialization and verify required columns and constraints."""
        db_obj = db.DB(self.db_path)
        self.assertTrue(os.path.exists(self.db_path))

        cursor = db_obj.conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [row["name"] for row in cursor.fetchall()]

        self.assertIn("schema_version", tables)
        self.assertIn("accounts", tables)
        self.assertIn("messages", tables)
        self.assertIn("proposals", tables)
        self.assertIn("decisions", tables)

        # Verify proposals columns include model_version and label_definition_version
        cursor.execute("PRAGMA table_info(proposals)")
        proposal_cols = {row["name"] for row in cursor.fetchall()}
        self.assertIn("model_version", proposal_cols)
        self.assertIn("prompt_version", proposal_cols)
        self.assertIn("label_definition_version", proposal_cols)
        self.assertIn("created_at", proposal_cols)

        # Verify messages columns and uniqueness on (account_id, gmail_message_id)
        cursor.execute("PRAGMA table_info(messages)")
        msg_cols = {row["name"] for row in cursor.fetchall()}
        self.assertIn("account_id", msg_cols)
        self.assertIn("gmail_message_id", msg_cols)
        self.assertIn("thread_id", msg_cols)
        # New columns added in schema v2 are additive and optional for Step 1 tests
        # (they may be present after migration)
        for optional in ("gmail_labels", "fetched_at", "truncated", "unsupported_content", "has_attachments"):
            # Just ensure we did not lose core columns; presence is not asserted here
            pass

        cursor.execute(
            "INSERT INTO accounts (id, name, email) VALUES (?, ?, ?)",
            ("acc-1", "Account One", "one@example.com"),
        )
        cursor.execute(
            "INSERT INTO accounts (id, name, email) VALUES (?, ?, ?)",
            ("acc-2", "Account Two", "two@example.com"),
        )
        cursor.execute(
            "INSERT INTO messages (id, account_id, gmail_message_id) VALUES (?, ?, ?)",
            ("m1", "acc-1", "gm-1"),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            cursor.execute(
                "INSERT INTO messages (id, account_id, gmail_message_id) VALUES (?, ?, ?)",
                ("m2", "acc-1", "gm-1"),
            )
        # Same Gmail id on a different account is allowed
        cursor.execute(
            "INSERT INTO messages (id, account_id, gmail_message_id) VALUES (?, ?, ?)",
            ("m3", "acc-2", "gm-1"),
        )

        db_obj.close()

    def test_database_persistence(self):
        """Test that restarting the database preserves records."""
        db_obj = db.DB(self.db_path)
        cursor = db_obj.conn.cursor()

        cursor.execute(
            """
            INSERT INTO accounts (id, name, email, gmail_id)
            VALUES (?, ?, ?, ?)
            """,
            ("acc-1", "Test Account", "user@example.com", "gmail-user-1"),
        )
        cursor.execute(
            """
            INSERT INTO messages (id, thread_id, account_id, gmail_message_id, subject, sender, sender_email)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("msg-1", "th-1", "acc-1", "gm-1", "Test Subject", "Sender", "sender@example.com"),
        )
        cursor.execute(
            """
            INSERT INTO proposals (id, message_id, account_id, label_ids, label_names, reason, confidence, source, model_version, prompt_version, label_definition_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "prop-1",
                "msg-1",
                "acc-1",
                '["Type/Receipt"]',
                '["Receipt"]',
                "Contains invoice",
                0.95,
                "model",
                "Qwen2.5-7B-Instruct-EXL3",
                "v1",
                "v1",
            ),
        )
        cursor.execute(
            """
            INSERT INTO decisions (id, message_id, account_id, status, label_ids, user_notes)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("dec-1", "msg-1", "acc-1", "accepted", '["Type/Receipt"]', "Confirmed invoice"),
        )
        db_obj.conn.commit()
        db_obj.close()

        # Reopen
        db_obj2 = db.DB(self.db_path)
        c2 = db_obj2.conn.cursor()
        c2.execute("SELECT COUNT(*) as count FROM accounts")
        self.assertEqual(c2.fetchone()["count"], 1)
        c2.execute("SELECT COUNT(*) as count FROM messages")
        self.assertEqual(c2.fetchone()["count"], 1)
        c2.execute("SELECT COUNT(*) as count FROM proposals")
        self.assertEqual(c2.fetchone()["count"], 1)
        c2.execute("SELECT COUNT(*) as count FROM decisions")
        self.assertEqual(c2.fetchone()["count"], 1)
        db_obj2.close()

    def test_config_initialization_and_defaults(self):
        """Test config creation and default values."""
        cfg = config_module.Config(self.config_path)
        self.assertTrue(os.path.exists(self.config_path))
        self.assertEqual(cfg.model_endpoint, "http://127.0.0.1:11434")
        self.assertEqual(cfg.model_id, "qwen2.5:7b")
        self.assertEqual(cfg.timeout, 30.0)
        self.assertEqual(cfg.sample_limit, 100)
        self.assertEqual(len(cfg.labels), 14)
        # Validation passes for defaults
        cfg.validate()

    def test_config_database_path_isolated(self):
        """Test that database_path is derived from the custom config location."""
        cfg = config_module.Config(self.config_path)
        expected_db = os.path.join(self.temp_dir, "EmailMan.db")
        self.assertEqual(cfg.database_path, expected_db)

    def test_default_app_dir_linux_and_windows(self):
        """Per-user data dir uses LOCALAPPDATA on Windows and XDG/HOME on Unix."""
        from unittest.mock import patch

        with patch.dict(os.environ, {"LOCALAPPDATA": self.temp_dir}, clear=False):
            self.assertEqual(
                config_module.default_app_dir(),
                Path(self.temp_dir) / "EmailMan",
            )
        env = {k: v for k, v in os.environ.items() if k != "LOCALAPPDATA"}
        env["XDG_CONFIG_HOME"] = self.temp_dir
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                config_module.default_app_dir(),
                Path(self.temp_dir) / "emailman",
            )
        env.pop("XDG_CONFIG_HOME", None)
        env["HOME"] = self.temp_dir
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                config_module.default_app_dir(),
                Path(self.temp_dir) / ".config" / "emailman",
            )

    def test_config_validation_loopback_enforced(self):
        """Test that non-loopback endpoints are strictly rejected and loopback accepted."""
        cfg = config_module.Config(self.config_path)

        # External host should raise ConfigError
        cfg._config["model"]["endpoint"] = "https://api.openai.com/v1"
        with self.assertRaises(config_module.ConfigError) as cm:
            cfg.validate()
        self.assertIn("loopback", str(cm.exception).lower())

        cfg._config["model"]["endpoint"] = "http://192.168.1.100:8080/completion"
        with self.assertRaises(config_module.ConfigError) as cm:
            cfg.validate()
        self.assertIn("loopback", str(cm.exception).lower())

        # Valid loopback endpoints
        valid_endpoints = [
            "http://127.0.0.1:8080/completion",
            "http://localhost:8080/completion",
            "http://[::1]:8080/completion",
        ]
        for ep in valid_endpoints:
            cfg._config["model"]["endpoint"] = ep
            cfg.validate()  # Should not raise

    def test_config_validation_errors(self):
        """Test validation catches missing fields and invalid limits."""
        cfg = config_module.Config(self.config_path)

        # Missing endpoint
        cfg._config["model"] = {"id": "test"}
        with self.assertRaises(config_module.ConfigError):
            cfg.validate()

        # Missing model ID
        cfg._config["model"] = {"endpoint": "http://127.0.0.1:8080"}
        with self.assertRaises(config_module.ConfigError):
            cfg.validate()

        # Invalid sample limit
        cfg._config["model"] = {"endpoint": "http://127.0.0.1:8080", "id": "test"}
        cfg._config["sample_limit"] = -5
        with self.assertRaises(config_module.ConfigError):
            cfg.validate()

        # Duplicate label ID
        cfg._config["sample_limit"] = 100
        cfg._config["labels"] = [
            {"id": "Type/A", "name": "A", "description": "desc", "exclusions": []},
            {"id": "Type/A", "name": "B", "description": "desc", "exclusions": []},
        ]
        with self.assertRaises(config_module.ConfigError):
            cfg.validate()

        # Duplicate label name
        cfg._config["labels"] = [
            {"id": "Type/A", "name": "Dup", "description": "desc", "exclusions": []},
            {"id": "Type/B", "name": "Dup", "description": "desc", "exclusions": []},
        ]
        with self.assertRaises(config_module.ConfigError):
            cfg.validate()

        # bool is not a valid sample_limit
        cfg._config["labels"] = [
            {"id": "Type/A", "name": "A", "description": "desc", "exclusions": []},
        ]
        cfg._config["sample_limit"] = True
        with self.assertRaises(config_module.ConfigError):
            cfg.validate()
        cfg._config["sample_limit"] = 100

        # exclusions must be a list
        cfg._config["labels"] = [
            {"id": "Type/A", "name": "A", "description": "desc", "exclusions": "not-a-list"},
        ]
        with self.assertRaises(config_module.ConfigError) as cm:
            cfg.validate()
        self.assertIn("exclusions", str(cm.exception).lower())

        # examples, if present, must be a list
        cfg._config["labels"] = [
            {
                "id": "Type/A",
                "name": "A",
                "description": "desc",
                "exclusions": [],
                "examples": "not-a-list",
            },
        ]
        with self.assertRaises(config_module.ConfigError) as cm:
            cfg.validate()
        self.assertIn("examples", str(cm.exception).lower())

        # empty labels list is invalid
        cfg._config["labels"] = []
        with self.assertRaises(config_module.ConfigError):
            cfg.validate()

    def test_flask_app_routes(self):
        """Test Flask routes via test_client."""
        cfg = config_module.Config(self.config_path)
        app = app_module.create_app(cfg)
        client = app.test_client()

        # Health endpoint
        res = client.get("/health")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["status"], "healthy")

        # Labels endpoint
        res = client.get("/api/labels")
        self.assertEqual(res.status_code, 200)
        labels = res.get_json()["labels"]
        self.assertEqual(len(labels), 14)

        # Root review page
        res = client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("EmailMan", res.get_data(as_text=True))

        # Status endpoint before DB initialization (must not crash)
        res = client.get("/api/status")
        self.assertEqual(res.status_code, 200)
        status_data = res.get_json()
        self.assertEqual(status_data["accounts"], 0)
        self.assertEqual(status_data["messages"], 0)
        self.assertFalse(status_data["db_initialized"])

        # Status endpoint after DB initialization
        db_obj = db.DB(cfg.database_path)
        db_obj.close()
        res = client.get("/api/status")
        self.assertEqual(res.status_code, 200)
        status_data2 = res.get_json()
        self.assertEqual(status_data2["accounts"], 0)
        self.assertTrue(status_data2["db_initialized"])

    def test_run_review_server_rejects_non_loopback_host(self):
        """Test that run_review_server rejects non-loopback host binding."""
        cfg = config_module.Config(self.config_path)
        with self.assertRaises(config_module.ConfigError) as cm:
            app_module.run_review_server(cfg, host="0.0.0.0")
        self.assertIn("loopback", str(cm.exception).lower())

    def test_run_review_server_forces_debug_off(self):
        """Finding 12: the review server never enables the Werkzeug debugger."""
        cfg = config_module.Config(self.config_path)
        cfg._config["debug"] = True
        fake_app = MagicMock()
        with patch.object(app_module, "create_app", return_value=fake_app):
            app_module.run_review_server(cfg, host="127.0.0.1", port=5000)
        fake_app.run.assert_called_once_with(
            host="127.0.0.1", port=5000, debug=False, use_reloader=False
        )

    def test_database_uses_wal_and_long_busy_timeout(self):
        """Finding 14: SQLite waits longer and allows readers during a writer."""
        cfg = config_module.Config(self.config_path)
        db_obj = db.DB(cfg.database_path)
        try:
            mode = db_obj.conn.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(str(mode).lower(), "wal")
            busy_ms = db_obj.conn.execute("PRAGMA busy_timeout").fetchone()[0]
            self.assertGreaterEqual(int(busy_ms), 30000)
        finally:
            db_obj.close()

    def test_synthetic_fixtures_parsing(self):
        """Test that synthetic email fixtures are valid RFC 2822 format and parseable."""
        fixtures_dir = Path(__file__).parent.parent / "EmailMan" / "fixtures"
        mail_files = ["mail_1.eml", "mail_2.eml"]

        for mf in mail_files:
            file_path = fixtures_dir / mf
            self.assertTrue(file_path.exists())
            with open(file_path, "rb") as f:
                msg = email.message_from_binary_file(f, policy=policy.default)
            self.assertIsNotNone(msg.get("Subject"))
            self.assertIsNotNone(msg.get("From"))
            self.assertIsNotNone(msg.get("To"))
            body = msg.get_body(preferencelist=("plain",))
            self.assertIsNotNone(body)
            self.assertTrue(len(body.get_content().strip()) > 0)

    def test_cli_init_db_with_custom_config(self):
        """Test CLI init-db initializes database at custom config location."""
        cfg = config_module.Config(self.config_path)
        parser = cli_module.create_cli_parser()
        args = parser.parse_args(["--config", self.config_path, "init-db"])
        exit_code = cli_module.init_db(args, cfg)
        self.assertEqual(exit_code, 0)
        self.assertTrue(os.path.exists(cfg.database_path))

    def test_config_get_dot_notation(self):
        """Test Config.get reads nested keys and returns the default when missing."""
        cfg = config_module.Config(self.config_path)
        self.assertEqual(cfg.get("model.endpoint"), "http://127.0.0.1:11434")
        self.assertEqual(cfg.get("model.id"), "qwen2.5:7b")
        self.assertIsInstance(cfg.get("model"), dict)
        self.assertIsNone(cfg.get("does.not.exist"))
        self.assertEqual(cfg.get("does.not.exist", "fallback"), "fallback")

    def test_config_missing_labels_uses_defaults(self):
        """Test omitted labels fall back to defaults for both property access and validation."""
        cfg = config_module.Config(self.config_path)
        del cfg._config["labels"]
        self.assertEqual(len(cfg.labels), 14)
        cfg.validate()

    def _assert_labels_consistent(self, labels):
        ids = [label["id"] for label in labels]
        names = [label["name"] for label in labels]
        self.assertEqual(len(ids), len(set(ids)), f"duplicate label id in {ids}")
        self.assertEqual(len(names), len(set(names)), f"duplicate label name in {names}")
        seen_examples = {}
        axes = set()
        for label in labels:
            for field in ("id", "name", "description", "examples", "exclusions", "axis"):
                self.assertTrue(label.get(field), f"{label.get('id')} missing {field}")
            self.assertIsInstance(label["examples"], list)
            self.assertIsInstance(label["exclusions"], list)
            self.assertIn(label["axis"], {"kind", "purchase", "retention"})
            axes.add(label["axis"])
            for example in label["examples"]:
                key = example.strip().lower()
                self.assertNotIn(
                    key,
                    seen_examples,
                    f"example {example!r} reused by {seen_examples.get(key)} and {label['id']}",
                )
                seen_examples[key] = label["id"]
        self.assertIn("kind", axes)
        self.assertIn("retention", axes)

    def test_config_labels_are_consistent(self):
        """Label definitions must not contradict each other or lose an axis.

        Regression for the pilot taxonomy: reusing one subject under several
        labels (and labels without an axis) taught the model to confuse them.
        """
        repo_root = Path(__file__).parent.parent
        with open(repo_root / "config.example.json", "r", encoding="utf-8") as f:
            example_labels = json.load(f)["labels"]
        sources = {
            "config.example.json": example_labels,
            "DEFAULT_LABELS": config_module.Config.DEFAULT_LABELS,
        }
        for source, labels in sources.items():
            with self.subTest(source=source):
                self._assert_labels_consistent(labels)

    def test_pending_proposals_count_by_message(self):
        """Test status counts distinct messages with proposals and no decision."""
        db_obj = db.DB(self.db_path)
        cursor = db_obj.conn.cursor()
        cursor.execute(
            "INSERT INTO accounts (id, name, email) VALUES (?, ?, ?)",
            ("acc-1", "Test Account", "user@example.com"),
        )
        cursor.execute(
            "INSERT INTO messages (id, account_id, gmail_message_id) VALUES (?, ?, ?)",
            ("msg-1", "acc-1", "gm-1"),
        )
        cursor.execute(
            "INSERT INTO messages (id, account_id, gmail_message_id) VALUES (?, ?, ?)",
            ("msg-2", "acc-1", "gm-2"),
        )
        cursor.execute(
            """
            INSERT INTO proposals (
                id, message_id, account_id, label_ids, source,
                model_version, prompt_version, label_definition_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("prop-1", "msg-1", "acc-1", "[]", "model", "mv", "pv", "lv"),
        )
        cursor.execute(
            """
            INSERT INTO proposals (
                id, message_id, account_id, label_ids, source,
                model_version, prompt_version, label_definition_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("prop-1b", "msg-1", "acc-1", "[]", "model", "mv2", "pv", "lv"),
        )
        cursor.execute(
            """
            INSERT INTO proposals (
                id, message_id, account_id, label_ids, source,
                model_version, prompt_version, label_definition_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("prop-2", "msg-2", "acc-1", "[]", "model", "mv", "pv", "lv"),
        )
        cursor.execute(
            """
            INSERT INTO decisions (id, message_id, account_id, status, label_ids)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("dec-1", "msg-1", "acc-1", "accepted", "[]"),
        )
        db_obj.conn.commit()
        db_obj.close()

        status = routes_module.get_status(self.db_path)
        self.assertEqual(status["accounts"], 1)
        self.assertEqual(status["messages"], 2)
        self.assertEqual(status["proposals_pending"], 1)
        self.assertEqual(status["decisions_pending"], 0)
        self.assertTrue(status["db_initialized"])

    def test_clear_cache_preserves_decisions(self):
        """Test clear-cache drops cached text and proposals but keeps decisions."""
        cfg = config_module.Config(self.config_path)
        db_obj = db.DB(cfg.database_path)
        cursor = db_obj.conn.cursor()
        cursor.execute(
            "INSERT INTO accounts (id, name, email) VALUES (?, ?, ?)",
            ("acc-1", "Test Account", "user@example.com"),
        )
        cursor.execute(
            """
            INSERT INTO messages (
                id, account_id, gmail_message_id, subject, body_preview
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("msg-1", "acc-1", "gm-1", "Invoice", "body secret"),
        )
        cursor.execute(
            """
            INSERT INTO proposals (
                id, message_id, account_id, label_ids, source,
                model_version, prompt_version, label_definition_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("prop-1", "msg-1", "acc-1", "[]", "model", "mv", "pv", "lv"),
        )
        cursor.execute(
            """
            INSERT INTO decisions (id, message_id, account_id, status, label_ids)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("dec-1", "msg-1", "acc-1", "accepted", '["Type/Receipt"]'),
        )
        db_obj.conn.commit()
        db_obj.close()

        parser = cli_module.create_cli_parser()
        args = parser.parse_args(["--config", self.config_path, "clear-cache"])
        exit_code = cli_module.clear_cache(args, cfg)
        self.assertEqual(exit_code, 0)

        db_obj = db.DB(cfg.database_path)
        cursor = db_obj.conn.cursor()
        self.assertEqual(cursor.execute("SELECT COUNT(*) as c FROM accounts").fetchone()["c"], 1)
        self.assertEqual(cursor.execute("SELECT COUNT(*) as c FROM messages").fetchone()["c"], 1)
        self.assertEqual(cursor.execute("SELECT COUNT(*) as c FROM proposals").fetchone()["c"], 0)
        self.assertEqual(cursor.execute("SELECT COUNT(*) as c FROM decisions").fetchone()["c"], 1)
        row = cursor.execute(
            "SELECT subject, body_preview, gmail_message_id FROM messages WHERE id = ?",
            ("msg-1",),
        ).fetchone()
        self.assertEqual(row["subject"], "Invoice")
        self.assertIsNone(row["body_preview"])
        self.assertEqual(row["gmail_message_id"], "gm-1")
        decision = cursor.execute("SELECT status, label_ids FROM decisions WHERE id = ?", ("dec-1",)).fetchone()
        self.assertEqual(decision["status"], "accepted")
        self.assertEqual(decision["label_ids"], '["Type/Receipt"]')
        db_obj.close()


if __name__ == "__main__":
    unittest.main()
