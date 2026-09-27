"""Tests for configurable scan_query and --documents-only shortcut (Issue #6).

Designed strictly around public black-box boundaries:
- CLI commands (`create-config`, `scan`) via CLI parser
- `config.json` on disk and public `Config` contract
- External Gmail fetch boundary (`fetch_bounded_sample(query=...)`)
- SQLite persistence (`account.last_query`)

Internal helper functions remain private implementation details and can be
freely refactored without breaking these tests.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from Mailroom import cli as cli_module
from Mailroom import db as db_module
from Mailroom import gmail as gmail_module
from Mailroom.config import Config, ConfigError


class TestCreateConfigScanQueryEndToEnd(unittest.TestCase):
    """Verify create-config generates expected config.json on disk."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = Path(self.temp_dir) / "config.json"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_create_config_default_standard_omits_scan_query_and_defaults_cleanly(self):
        """Standard archetype creates minimal config without scan_query key, defaulting cleanly."""
        parser = cli_module.create_cli_parser()
        args = parser.parse_args(["create-config", "--config", str(self.config_path), "--taxonomy", "standard"])
        code = cli_module.create_config_cmd(args)
        self.assertEqual(code, 0)
        self.assertTrue(self.config_path.exists())

        raw_data = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertNotIn("scan_query", raw_data)

        cfg = Config(str(self.config_path))
        cfg.validate()
        self.assertEqual(cfg.scan_query, "in:inbox -in:trash -in:spam -in:drafts")

    def test_create_config_single_label_writes_documents_attachment_query(self):
        """Single-label archetype writes document attachment scan_query to config.json."""
        parser = cli_module.create_cli_parser()
        args = parser.parse_args(["create-config", "--config", str(self.config_path), "--taxonomy", "single-label"])
        code = cli_module.create_config_cmd(args)
        self.assertEqual(code, 0)
        self.assertTrue(self.config_path.exists())

        raw_data = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(raw_data.get("scan_query"), "has:attachment (filename:pdf OR filename:xml)")

        cfg = Config(str(self.config_path))
        cfg.validate()
        self.assertEqual(cfg.scan_query, "has:attachment (filename:pdf OR filename:xml)")

    def test_create_config_single_label_custom_target_label(self):
        """Single-label with custom --target-label preserves document scan_query and label ID."""
        parser = cli_module.create_cli_parser()
        args = parser.parse_args(
            [
                "create-config",
                "--config",
                str(self.config_path),
                "--taxonomy",
                "single-label",
                "--target-label",
                "Invoices",
            ]
        )
        code = cli_module.create_config_cmd(args)
        self.assertEqual(code, 0)
        self.assertTrue(self.config_path.exists())

        raw_data = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(raw_data.get("scan_query"), "has:attachment (filename:pdf OR filename:xml)")
        labels = raw_data.get("labels", [])
        self.assertEqual(len(labels), 1)
        self.assertEqual(labels[0]["id"], "Invoices")


class TestConfigScanQueryPublicContract(unittest.TestCase):
    """Verify Config public contract and validation for scan_query."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = Path(self.temp_dir) / "config.json"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _write_config(self, extra_fields: dict) -> Config:
        base = {
            "model": {
                "endpoint": "http://127.0.0.1:11434",
                "id": "qwen2.5:7b",
                "provider": "ollama",
            },
            "timeout": 30.0,
            "sample_limit": 100,
            "labels": Config.DEFAULT_LABELS,
            "debug": False,
        }
        base.update(extra_fields)
        self.config_path.write_text(json.dumps(base), encoding="utf-8")
        return Config(str(self.config_path))

    def test_config_missing_scan_query_defaults_to_inbox_query(self):
        """Omitting scan_query from config.json falls back to DEFAULT_SCAN_QUERY."""
        cfg = self._write_config({})
        cfg.validate()
        self.assertEqual(cfg.scan_query, "in:inbox -in:trash -in:spam -in:drafts")

    def test_config_custom_scan_query_preserved_and_valid(self):
        """Custom scan_query string is preserved and passes validation."""
        cfg = self._write_config({"scan_query": "label:custom-invoices"})
        cfg.validate()
        self.assertEqual(cfg.scan_query, "label:custom-invoices")

    def test_config_scan_query_validation_rejects_invalid_values(self):
        """Non-string or empty scan_query values raise ConfigError."""
        invalid_values = [
            None,
            "",
            "   ",
            123,
            True,
            False,
            ["has:attachment"],
            {"query": "test"},
        ]
        for val in invalid_values:
            cfg = self._write_config({"scan_query": val})
            with self.assertRaises(ConfigError, msg=f"Should raise ConfigError for {val!r}"):
                cfg.validate()


class TestScanQueryResolutionEndToEnd(unittest.TestCase):
    """Verify scan command resolution order and observable boundaries."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = Path(self.temp_dir) / "config.json"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_config_file(self, scan_query=None):
        base = {
            "model": {
                "endpoint": "http://127.0.0.1:11434",
                "id": "qwen2.5:7b",
                "provider": "ollama",
            },
            "timeout": 30.0,
            "sample_limit": 50,
            "labels": Config.DEFAULT_LABELS,
            "debug": False,
        }
        if scan_query is not None:
            base["scan_query"] = scan_query
        self.config_path.write_text(json.dumps(base), encoding="utf-8")
        return Config(str(self.config_path))

    def _run_scan(self, cli_args, config, sample_time="2026-09-27T20:00:00+00:00"):
        parser = cli_module.create_cli_parser()
        parsed_args = parser.parse_args(["--config", str(self.config_path)] + cli_args)
        catalog = [{"id": "INBOX", "name": "INBOX", "type": "system"}]

        with patch.object(gmail_module, "get_gmail_service", return_value=(object(), "user@example.com")), \
             patch.object(gmail_module, "fetch_account_and_labels", return_value=({"emailAddress": "user@example.com"}, catalog)), \
             patch.object(gmail_module, "fetch_bounded_sample", return_value=([], sample_time)) as mock_fetch:
            code = cli_module.scan_cmd(parsed_args, config)

        db_obj = db_module.DB(config.database_path)
        acc = db_obj.get_account("user@example.com")
        db_obj.close()
        return code, mock_fetch, acc

    def test_scan_default_inbox_query(self):
        """Default scan with no flags uses standard inbox query."""
        config = self._create_config_file(scan_query=None)
        code, mock_fetch, acc = self._run_scan(["scan"], config)

        self.assertEqual(code, 0)
        expected = "in:inbox -in:trash -in:spam -in:drafts"
        self.assertEqual(mock_fetch.call_args[1]["query"], expected)
        self.assertEqual(acc["last_query"], expected)

    def test_scan_uses_config_scan_query_when_set(self):
        """Scan respects scan_query configured in config.json."""
        config = self._create_config_file(scan_query="label:finance")
        code, mock_fetch, acc = self._run_scan(["scan"], config)

        self.assertEqual(code, 0)
        expected = "label:finance"
        self.assertEqual(mock_fetch.call_args[1]["query"], expected)
        self.assertEqual(acc["last_query"], expected)

    def test_scan_cli_query_overrides_config_scan_query(self):
        """Explicit --query overrides config.scan_query."""
        config = self._create_config_file(scan_query="label:finance")
        code, mock_fetch, acc = self._run_scan(["scan", "--query", "from:boss@example.com"], config)

        self.assertEqual(code, 0)
        expected = "from:boss@example.com"
        self.assertEqual(mock_fetch.call_args[1]["query"], expected)
        self.assertEqual(acc["last_query"], expected)

    def test_scan_documents_only_appends_to_default_query(self):
        """--documents-only appends document filter to default query."""
        config = self._create_config_file(scan_query=None)
        code, mock_fetch, acc = self._run_scan(["scan", "--documents-only"], config)

        self.assertEqual(code, 0)
        expected = "in:inbox -in:trash -in:spam -in:drafts has:attachment (filename:pdf OR filename:xml)"
        self.assertEqual(mock_fetch.call_args[1]["query"], expected)
        self.assertEqual(acc["last_query"], expected)

    def test_scan_documents_only_appends_to_config_query(self):
        """--documents-only appends document filter to custom config.scan_query."""
        config = self._create_config_file(scan_query="label:unread")
        code, mock_fetch, acc = self._run_scan(["scan", "--documents-only"], config)

        self.assertEqual(code, 0)
        expected = "label:unread has:attachment (filename:pdf OR filename:xml)"
        self.assertEqual(mock_fetch.call_args[1]["query"], expected)
        self.assertEqual(acc["last_query"], expected)

    def test_scan_documents_only_appends_to_explicit_cli_query(self):
        """--documents-only appends document filter to explicit CLI --query."""
        config = self._create_config_file(scan_query="label:ignored")
        code, mock_fetch, acc = self._run_scan(["scan", "--query", "label:receipts", "--documents-only"], config)

        self.assertEqual(code, 0)
        expected = "label:receipts has:attachment (filename:pdf OR filename:xml)"
        self.assertEqual(mock_fetch.call_args[1]["query"], expected)
        self.assertEqual(acc["last_query"], expected)

    def test_scan_documents_only_deduplicates_if_already_present(self):
        """--documents-only does not append filter if already present in base query."""
        config = self._create_config_file(scan_query="has:attachment (filename:pdf OR filename:xml)")
        code, mock_fetch, acc = self._run_scan(["scan", "--documents-only"], config)

        self.assertEqual(code, 0)
        expected = "has:attachment (filename:pdf OR filename:xml)"
        self.assertEqual(mock_fetch.call_args[1]["query"], expected)
        self.assertEqual(acc["last_query"], expected)

    def test_scan_per_month_with_documents_only(self):
        """--per-month with --documents-only applies document filter to monthly base query."""
        config = self._create_config_file(scan_query=None)
        code, mock_fetch, acc = self._run_scan(
            ["scan", "--per-month", "2", "--years", "1", "--documents-only"],
            config,
        )

        self.assertEqual(code, 0)
        self.assertTrue(mock_fetch.call_count > 0)
        for call in mock_fetch.call_args_list:
            query = call[1]["query"]
            self.assertIn("has:attachment (filename:pdf OR filename:xml)", query)


class TestSingleLabelInvoiceWorkflowJourney(unittest.TestCase):
    """End-to-end user journey: create single-label config then scan without query flags."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = Path(self.temp_dir) / "config.json"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_single_label_invoice_journey_scans_documents_seamlessly(self):
        # Step 1: User initializes single-label invoice config
        parser = cli_module.create_cli_parser()
        init_args = parser.parse_args(["create-config", "--config", str(self.config_path), "--taxonomy", "single-label"])
        code = cli_module.create_config_cmd(init_args)
        self.assertEqual(code, 0)

        # Step 2: System loads created configuration
        config = Config(str(self.config_path))
        self.assertEqual(config.scan_query, "has:attachment (filename:pdf OR filename:xml)")

        # Step 3: User runs mailroom scan with no query flags
        scan_args = parser.parse_args(["--config", str(self.config_path), "scan"])
        catalog = [{"id": "INBOX", "name": "INBOX", "type": "system"}]
        sample_time = "2026-09-27T21:00:00+00:00"

        with patch.object(gmail_module, "get_gmail_service", return_value=(object(), "invoices@example.com")), \
             patch.object(gmail_module, "fetch_account_and_labels", return_value=({"emailAddress": "invoices@example.com"}, catalog)), \
             patch.object(gmail_module, "fetch_bounded_sample", return_value=([], sample_time)) as mock_fetch:
            scan_code = cli_module.scan_cmd(scan_args, config)

        # Step 4: Verify boundary invocations and database record
        self.assertEqual(scan_code, 0)
        expected_query = "has:attachment (filename:pdf OR filename:xml)"
        self.assertEqual(mock_fetch.call_args[1]["query"], expected_query)

        db_obj = db_module.DB(config.database_path)
        acc = db_obj.get_account("invoices@example.com")
        db_obj.close()
        self.assertEqual(acc["last_query"], expected_query)
