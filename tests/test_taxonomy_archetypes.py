"""Tests for single-target taxonomy archetypes and custom template files (Issue #5)."""

import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from Mailroom import cli as cli_module
from Mailroom.classifier import EmailClassifier
from Mailroom.classification import Proposal
from Mailroom.config import Config


class TestCreateConfigCliE2E(unittest.TestCase):
    """End-to-end tests for CLI config creation across taxonomy archetypes and templates."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = Path(self.temp_dir) / "config.json"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_create_config_default_creates_valid_standard_taxonomy(self):
        """create-config with no taxonomy flags writes valid standard 12-label taxonomy."""
        args = cli_module.create_cli_parser().parse_args(
            ["create-config", "--config", str(self.config_path)]
        )
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            ret = cli_module.create_config_cmd(args)

        self.assertEqual(ret, 0)
        stdout = out.getvalue()
        self.assertIn("Configuration created", stdout)
        self.assertIn("Next steps (local-first; no Gmail account needed):", stdout)
        self.assertTrue(self.config_path.exists())

        cfg = Config(str(self.config_path))
        cfg.validate()
        self.assertEqual(len(cfg.labels), len(Config.DEFAULT_LABELS))
        axes = {label.get("axis") for label in cfg.labels}
        self.assertTrue({"kind", "purchase", "retention"}.issubset(axes))

    def test_create_config_single_label_default_creates_rechnungen_taxonomy(self):
        """create-config --taxonomy single-label writes the Rechnungen archetype."""
        args = cli_module.create_cli_parser().parse_args(
            ["create-config", "--config", str(self.config_path), "--taxonomy", "single-label"]
        )
        with patch("sys.stdout", new_callable=io.StringIO):
            ret = cli_module.create_config_cmd(args)

        self.assertEqual(ret, 0)
        self.assertTrue(self.config_path.exists())

        cfg = Config(str(self.config_path))
        cfg.validate()
        self.assertEqual(len(cfg.labels), 1)
        label = cfg.labels[0]
        self.assertEqual(label["id"], "Rechnungen")
        self.assertEqual(label["name"], "Rechnungen")
        self.assertEqual(label["axis"], "kind")
        self.assertIn("Invoice", label["description"])
        self.assertIn("Rechnung", label["examples"])
        self.assertIn("Newsletter or promotional deal", label["exclusions"])

    def test_create_config_single_label_with_target_label_override(self):
        """--target-label overrides the single-label ID and display name."""
        args = cli_module.create_cli_parser().parse_args(
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
        with patch("sys.stdout", new_callable=io.StringIO):
            ret = cli_module.create_config_cmd(args)

        self.assertEqual(ret, 0)
        cfg = Config(str(self.config_path))
        cfg.validate()
        self.assertEqual(len(cfg.labels), 1)
        self.assertEqual(cfg.labels[0]["id"], "Invoices")
        self.assertEqual(cfg.labels[0]["name"], "Invoices")

    def test_create_config_custom_template_json_array(self):
        """create-config --template loads a JSON array of label definitions."""
        custom_labels = [
            {
                "id": "Doc/Contract",
                "name": "Contract",
                "axis": "document",
                "description": "Signed legal agreement",
                "examples": ["NDA", "Employment agreement"],
                "exclusions": ["Receipt", "Invoice"],
            },
            {
                "id": "Doc/Tax",
                "name": "Tax",
                "axis": "document",
                "description": "Tax declaration or assessment",
                "examples": ["Steuerbescheid"],
                "exclusions": ["Newsletter"],
            },
        ]
        template_file = Path(self.temp_dir) / "custom_array.json"
        template_file.write_text(json.dumps(custom_labels), encoding="utf-8")

        args = cli_module.create_cli_parser().parse_args(
            ["create-config", "--config", str(self.config_path), "--template", str(template_file)]
        )
        with patch("sys.stdout", new_callable=io.StringIO):
            ret = cli_module.create_config_cmd(args)

        self.assertEqual(ret, 0)
        cfg = Config(str(self.config_path))
        cfg.validate()
        self.assertEqual(len(cfg.labels), 2)
        self.assertEqual(cfg.get_label_ids(), ["Doc/Contract", "Doc/Tax"])

    def test_create_config_custom_template_wrapped_labels_dict(self):
        """create-config --template loads a JSON object with a 'labels' key."""
        custom_dict = {
            "labels": [
                {
                    "id": "Custom/One",
                    "name": "One",
                    "axis": "custom",
                    "description": "Custom label one",
                    "examples": ["Example one"],
                    "exclusions": ["Exclusion one"],
                }
            ]
        }
        template_file = Path(self.temp_dir) / "custom_wrapped.json"
        template_file.write_text(json.dumps(custom_dict), encoding="utf-8")

        args = cli_module.create_cli_parser().parse_args(
            ["create-config", "--config", str(self.config_path), "--template", str(template_file)]
        )
        with patch("sys.stdout", new_callable=io.StringIO):
            ret = cli_module.create_config_cmd(args)

        self.assertEqual(ret, 0)
        cfg = Config(str(self.config_path))
        cfg.validate()
        self.assertEqual(len(cfg.labels), 1)
        self.assertEqual(cfg.labels[0]["id"], "Custom/One")

    def test_create_config_existing_file_not_overwritten(self):
        """create-config will not overwrite an existing configuration file."""
        self.config_path.write_text(json.dumps({"sentinel": True}), encoding="utf-8")
        args = cli_module.create_cli_parser().parse_args(
            ["create-config", "--config", str(self.config_path), "--taxonomy", "single-label"]
        )
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            ret = cli_module.create_config_cmd(args)

        self.assertEqual(ret, 0)
        self.assertIn("Configuration file already exists", out.getvalue())
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(data, {"sentinel": True})


class TestCreateConfigResilienceAndFailureE2E(unittest.TestCase):
    """Failure invariants: bad flags or files exit non-zero and leave disk untouched."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = Path(self.temp_dir) / "config.json"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_target_label_without_single_label_taxonomy_fails_cleanly(self):
        """--target-label with standard taxonomy exits non-zero and leaves no files."""
        args = cli_module.create_cli_parser().parse_args(
            [
                "create-config",
                "--config",
                str(self.config_path),
                "--taxonomy",
                "standard",
                "--target-label",
                "Invoices",
            ]
        )
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            ret = cli_module.create_config_cmd(args)

        self.assertNotEqual(ret, 0)
        self.assertIn("--target-label requires --taxonomy single-label", err.getvalue())
        self.assertFalse(self.config_path.exists())
        self.assertEqual(list(Path(self.temp_dir).glob("*.tmp")), [])

    def test_empty_or_whitespace_target_label_fails_cleanly(self):
        """Empty or all-whitespace target label exits non-zero and leaves no files."""
        for bad_label in ["", "   ", "\t\n"]:
            with self.subTest(bad_label=bad_label):
                cfg_path = Path(self.temp_dir) / f"config_{abs(hash(bad_label))}.json"
                args = cli_module.create_cli_parser().parse_args(
                    [
                        "create-config",
                        "--config",
                        str(cfg_path),
                        "--taxonomy",
                        "single-label",
                        "--target-label",
                        bad_label,
                    ]
                )
                with patch("sys.stderr", new_callable=io.StringIO) as err:
                    ret = cli_module.create_config_cmd(args)

                self.assertNotEqual(ret, 0)
                self.assertIn("cannot be empty", err.getvalue().lower())
                self.assertFalse(cfg_path.exists())
                self.assertEqual(list(Path(self.temp_dir).glob("*.tmp")), [])

    def test_single_label_taxonomy_empty_string_raises(self):
        """single_label_taxonomy raises ConfigError for empty or whitespace strings."""
        from Mailroom.config import single_label_taxonomy, ConfigError

        for bad in ["", "   ", "\t"]:
            with self.subTest(bad=bad):
                with self.assertRaises(ConfigError) as ctx:
                    single_label_taxonomy(bad)
                self.assertIn("cannot be empty", str(ctx.exception).lower())

    def test_conflicting_template_and_taxonomy_flags_fails_cleanly(self):
        """Specifying --template and --taxonomy single-label exits non-zero."""
        args = cli_module.create_cli_parser().parse_args(
            [
                "create-config",
                "--config",
                str(self.config_path),
                "--template",
                str(Path(self.temp_dir) / "any.json"),
                "--taxonomy",
                "single-label",
            ]
        )
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            ret = cli_module.create_config_cmd(args)

        self.assertNotEqual(ret, 0)
        self.assertIn("cannot be combined", err.getvalue().lower())
        self.assertFalse(self.config_path.exists())

    def test_conflicting_template_and_target_label_flags_fails_cleanly(self):
        """Specifying --template and --target-label exits non-zero."""
        args = cli_module.create_cli_parser().parse_args(
            [
                "create-config",
                "--config",
                str(self.config_path),
                "--template",
                str(Path(self.temp_dir) / "any.json"),
                "--target-label",
                "Invoices",
            ]
        )
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            ret = cli_module.create_config_cmd(args)

        self.assertNotEqual(ret, 0)
        self.assertIn("cannot be combined", err.getvalue().lower())
        self.assertFalse(self.config_path.exists())

    def test_remote_template_url_rejected_without_network_or_disk(self):
        """Remote URLs in --template are rejected immediately with no network or disk writes."""
        args = cli_module.create_cli_parser().parse_args(
            [
                "create-config",
                "--config",
                str(self.config_path),
                "--template",
                "https://example.com/labels.json",
            ]
        )
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            ret = cli_module.create_config_cmd(args)

        self.assertNotEqual(ret, 0)
        self.assertIn("remote template urls are not supported", err.getvalue().lower())
        self.assertFalse(self.config_path.exists())

    def test_nonexistent_template_file_fails_cleanly(self):
        """Non-existent template path exits non-zero with informative error."""
        nonexistent = Path(self.temp_dir) / "does_not_exist.json"
        args = cli_module.create_cli_parser().parse_args(
            ["create-config", "--config", str(self.config_path), "--template", str(nonexistent)]
        )
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            ret = cli_module.create_config_cmd(args)

        self.assertNotEqual(ret, 0)
        self.assertIn("does not exist", err.getvalue().lower())
        self.assertFalse(self.config_path.exists())

    def test_malformed_json_template_fails_cleanly(self):
        """Malformed JSON template exits non-zero without writing config."""
        broken = Path(self.temp_dir) / "broken.json"
        broken.write_text("{ 'labels': [ not valid json", encoding="utf-8")
        args = cli_module.create_cli_parser().parse_args(
            ["create-config", "--config", str(self.config_path), "--template", str(broken)]
        )
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            ret = cli_module.create_config_cmd(args)

        self.assertNotEqual(ret, 0)
        self.assertIn("invalid json", err.getvalue().lower())
        self.assertFalse(self.config_path.exists())

    def test_invalid_label_schema_template_fails_cleanly(self):
        """Template with schema violations (missing description) exits non-zero."""
        invalid_schema = Path(self.temp_dir) / "invalid.json"
        invalid_schema.write_text(
            json.dumps([{"id": "Doc/Test", "name": "Test", "exclusions": []}]),
            encoding="utf-8",
        )
        args = cli_module.create_cli_parser().parse_args(
            ["create-config", "--config", str(self.config_path), "--template", str(invalid_schema)]
        )
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            ret = cli_module.create_config_cmd(args)

        self.assertNotEqual(ret, 0)
        self.assertIn("missing required field", err.getvalue().lower())
        self.assertFalse(self.config_path.exists())


class TestSingleLabelClassificationE2E(unittest.TestCase):
    """End-to-end tests for classification against single-label configuration."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = Path(self.temp_dir) / "config.json"
        # Pre-seed single-label config
        args = cli_module.create_cli_parser().parse_args(
            ["create-config", "--config", str(self.config_path), "--taxonomy", "single-label"]
        )
        with patch("sys.stdout", new_callable=io.StringIO):
            cli_module.create_config_cmd(args)
        self.config = Config(str(self.config_path))

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @patch("Mailroom.classification.requests.Session")
    def test_email_classifier_single_label_match(self, mock_session_cls):
        """Classifier correctly parses and returns single-label match proposal."""
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {
            "model": "qwen2.5:3b",
            "response": json.dumps(
                {
                    "label_ids": ["Rechnungen"],
                    "reason": "Order invoice",
                    "abstain": False,
                }
            ),
        }
        mock_session = mock_session_cls.return_value
        mock_session.post.return_value = mock_response

        classifier = EmailClassifier(config=self.config)
        proposal = classifier.classify(subject="Invoice #123", body="Total: $45.00")

        self.assertIsInstance(proposal, Proposal)
        self.assertEqual(proposal.label_ids, ["Rechnungen"])
        self.assertEqual(proposal.reason, "Order invoice")
        self.assertFalse(proposal.abstain)

    @patch("Mailroom.classification.requests.Session")
    def test_email_classifier_single_label_non_match_empty_labels(self, mock_session_cls):
        """Classifier returns empty label_ids and abstain=False when non-matching email occurs."""
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {
            "model": "qwen2.5:3b",
            "response": json.dumps(
                {
                    "label_ids": [],
                    "reason": "Newsletter digest",
                    "abstain": False,
                }
            ),
        }
        mock_session = mock_session_cls.return_value
        mock_session.post.return_value = mock_response

        classifier = EmailClassifier(config=self.config)
        proposal = classifier.classify(subject="Weekly News", body="Top stories of the week")

        self.assertEqual(proposal.label_ids, [])
        self.assertEqual(proposal.reason, "Newsletter digest")
        self.assertFalse(proposal.abstain)

    @patch("Mailroom.classification.requests.Session")
    def test_email_classifier_single_label_ambiguous_abstain(self, mock_session_cls):
        """Classifier returns abstain=True when model response indicates uncertainty."""
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {
            "model": "qwen2.5:3b",
            "response": json.dumps(
                {
                    "label_ids": [],
                    "reason": "Uncertain shipping status",
                    "abstain": True,
                }
            ),
        }
        mock_session = mock_session_cls.return_value
        mock_session.post.return_value = mock_response

        classifier = EmailClassifier(config=self.config)
        proposal = classifier.classify(subject="Update", body="Your package might arrive soon")

        self.assertEqual(proposal.label_ids, [])
        self.assertTrue(proposal.abstain)

    @patch("Mailroom.classification.requests.Session")
    def test_cli_classify_text_single_label_end_to_end(self, mock_session_cls):
        """mailroom classify --text against single-label config emits valid proposal JSON."""
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {
            "model": "qwen2.5:7b",
            "response": json.dumps(
                {
                    "label_ids": ["Rechnungen"],
                    "reason": "Rechnung",
                    "abstain": False,
                }
            ),
        }
        mock_session = mock_session_cls.return_value
        mock_session.post.return_value = mock_response

        args = cli_module.create_cli_parser().parse_args(
            ["--config", str(self.config_path), "classify", "--text", "Subject: Rechnung\n\nTotal 20 EUR"]
        )
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            ret = cli_module.classify_cmd(args, self.config)

        self.assertEqual(ret, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["label_ids"], ["Rechnungen"])
        self.assertEqual(payload["label_names"], ["Rechnungen"])
        self.assertFalse(payload["abstain"])
        self.assertEqual(payload["reason"], "Rechnung")


class TestTaxonomyLifecycleIntegrationE2E(unittest.TestCase):
    """Full lifecycle: create config -> validate -> execute classification."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = Path(self.temp_dir) / "config.json"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @patch("Mailroom.classification.requests.Session")
    def test_create_config_to_classification_journey(self, mock_session_cls):
        """End-to-end journey from create-config to classification completes with 0 errors."""
        # Step 1: Generate config via CLI
        args = cli_module.create_cli_parser().parse_args(
            [
                "create-config",
                "--config",
                str(self.config_path),
                "--taxonomy",
                "single-label",
                "--target-label",
                "Belege",
            ]
        )
        with patch("sys.stdout", new_callable=io.StringIO):
            ret = cli_module.create_config_cmd(args)
        self.assertEqual(ret, 0)

        # Step 2: Load config
        cfg = Config(str(self.config_path))
        cfg.validate()
        self.assertEqual(cfg.get_label_ids(), ["Belege"])

        # Step 3: Classify email
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {
            "model": "qwen2.5:3b",
            "response": json.dumps(
                {
                    "label_ids": ["Belege"],
                    "reason": "Beleg",
                    "abstain": False,
                }
            ),
        }
        mock_session = mock_session_cls.return_value
        mock_session.post.return_value = mock_response

        classifier = EmailClassifier(config=cfg)
        proposal = classifier.classify(
            subject="Beleg #456",
            body="Betrag: 10 EUR",
        )
        self.assertEqual(proposal.label_ids, ["Belege"])
        self.assertFalse(proposal.abstain)
        self.assertEqual(proposal.reason, "Beleg")


if __name__ == "__main__":
    unittest.main()
