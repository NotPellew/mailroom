"""Command-line interface for Mailroom."""

import sys
import json
import logging
import queue
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


def create_cli_parser():
    """Create command-line argument parser.

    Returns:
        Configured argument parser
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="mailroom",
        description="Mailroom - local-first mail label suggestion, review, and export (Gmail sync optional)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Local-first workflow (no Gmail account needed):
  setup           Interactive onboarding wizard for first-time configuration
  create-config   Create the configuration file
  init-db         Initialize the database
  ingest          Ingest local .eml files or directories into SQLite
  classify        Classify cached mail or direct text with the local model
  review          Start the local review server for corrections (alias: run-server)
  export          Export reviewed choices as CSV or JSON
  suggest-labels  Propose a label vocabulary from cached mail (local model)
  pilot-report    Review metrics and label-conflict report (local model commentary)
  sync-review     Export reviews and resolve label conflicts in the UI
  doctor          Check environment health, backend availability, and database status
  fix-schema      Verify and upgrade database schema
  clear-cache     Clear cached message text and proposals (keeps decisions)
  reset-db        Reset database (delete all data)

Optional Gmail sync (needs OAuth credentials and the [gmail] extra):
  auth            Authenticate with Gmail (read-only OAuth; stores refresh token securely)
  scan            Fetch a bounded Gmail sample and store it for review
                  (optional --classify runs local inference during download)
  apply-labels    Apply reviewed labels in Gmail (dry run unless --apply is passed)
  sync-labels     Read Gmail labels back for applied mail (read-only pull)
        """,
    )

    parser.add_argument(
        "--config",
        default=None,
        help="Path to configuration file (default: per-user Mailroom dir)",
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # setup
    setup_parser = subparsers.add_parser(
        "setup",
        help="Interactive onboarding wizard: configure hardware profile, taxonomy, database, and backend",
    )
    setup_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Exit cleanly with instructions if run non-interactively or in automated scripts",
    )

    # doctor
    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Check environment health: loopback accessibility, backend availability, and database status",
    )
    doctor_parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit with non-zero status if optional dependencies or backend are not ready",
    )

    # review
    review = subparsers.add_parser("review", help="Start the review server")
    review.add_argument("--host", default="127.0.0.1", help="Host to bind to (loopback only, default: 127.0.0.1)")
    review.add_argument("--port", type=int, default=5000, help="Port to bind to (default: 5000)")

    # run-server (alias for review)
    run_server = subparsers.add_parser("run-server", help="Start the review server (alias for review)")
    run_server.add_argument("--host", default="127.0.0.1", help="Host to bind to (loopback only, default: 127.0.0.1)")
    run_server.add_argument("--port", type=int, default=5000, help="Port to bind to (default: 5000)")

    # init-db
    subparsers.add_parser("init-db", help="Initialize the database")

    # fix-schema
    subparsers.add_parser("fix-schema", help="Fix/verify database schema")

    # reset-db
    subparsers.add_parser("reset-db", help="Reset database (delete all data)")

    # clear-cache
    subparsers.add_parser(
        "clear-cache",
        help="Clear cached message text and proposals (keeps accounts and decisions)",
    )

    # ingest
    ingest_parser = subparsers.add_parser(
        "ingest", help="Ingest local .eml files or directories into SQLite (primary, Gmail-free path)"
    )
    ingest_parser.add_argument("path", help="Path to an .eml file or directory of .eml files")
    ingest_parser.add_argument(
        "--account",
        default=None,
        help="Account ID to associate with ingested messages (default: 'local')",
    )
    ingest_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of files to ingest",
    )
    ingest_parser.add_argument(
        "--no-recursive",
        dest="recursive",
        action="store_false",
        default=True,
        help="Do not search directories recursively",
    )

    # create-config
    create_config_parser = subparsers.add_parser("create-config", help="Create initial configuration file")
    create_config_parser.add_argument(
        "--config",
        default=argparse.SUPPRESS,
        help="Path to configuration file (default: per-user Mailroom dir)",
    )
    create_config_parser.add_argument(
        "--profile",
        choices=["tabby", "standard", "lightweight"],
        default="standard",
        help="Hardware profile (standard for Ollama, lightweight for smaller Ollama models, tabby for TabbyAPI)",
    )
    create_config_parser.add_argument(
        "--taxonomy",
        choices=["standard", "single-label"],
        default="standard",
        help="Taxonomy archetype: 'standard' (14-label multi-axis) or 'single-label' (focused single target)",
    )
    create_config_parser.add_argument(
        "--target-label",
        type=str,
        default=None,
        help="Custom label ID and display name when using --taxonomy single-label (default: Rechnungen)",
    )
    create_config_parser.add_argument(
        "--template",
        type=str,
        default=None,
        help="Path to a local JSON file containing custom label definitions",
    )

    # auth
    auth_parser = subparsers.add_parser(
        "auth", help="Optional Gmail sync: authenticate with Gmail (read-only; no Gmail account needed for local ingest)"
    )
    auth_parser.add_argument("--reauth", action="store_true", help="Force reauthentication even if a token exists")

    # scan
    from Mailroom.gmail import DEFAULT_GMAIL_QUERY

    scan_parser = subparsers.add_parser(
        "scan", help="Optional Gmail sync: fetch a bounded Gmail sample and store messages"
    )
    scan_parser.add_argument(
        "--query",
        default=None,
        help=f"Gmail search query (overrides config scan_query; default: config scan_query or {DEFAULT_GMAIL_QUERY})",
    )
    scan_parser.add_argument(
        "--documents-only",
        action="store_true",
        help="Filter for messages with document attachments: ensures 'has:attachment (filename:pdf OR filename:xml)' is included in the query",
    )
    scan_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum messages to fetch (default: config sample_limit, capped at 1000)",
    )
    scan_parser.add_argument(
        "--classify",
        action="store_true",
        help="Classify each downloaded message with the local model while Gmail fetch continues",
    )
    scan_parser.add_argument(
        "--suggest-labels",
        action="store_true",
        dest="suggest_labels",
        help="Invent a label vocabulary from downloaded mail while Gmail fetch continues",
    )
    scan_parser.add_argument(
        "--batch-size",
        type=int,
        default=8,
        help="Messages per suggest-labels model call (default 8, max 20)",
    )
    scan_parser.add_argument(
        "--per-month",
        type=int,
        default=None,
        help="Fetch this many messages from each month instead of one newest sample",
    )
    scan_parser.add_argument(
        "--years",
        type=int,
        default=3,
        help="With --per-month, how many years back (default 3)",
    )

    # classify
    classify_parser = subparsers.add_parser(
        "classify",
        help="Classify a message locally (cached mail, or direct text/file/stdin)",
    )
    classify_parser.add_argument(
        "--message-id",
        type=str,
        default=None,
        help="Message ID to classify (format: account_id:message_id)",
    )
    classify_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Classify up to N cached messages (max 1000)",
    )
    classify_parser.add_argument(
        "--reclassify",
        action="store_true",
        help="Re-score messages that already have a proposal (latest proposal wins)",
    )
    classify_parser.add_argument(
        "--offset",
        type=int,
        default=0,
        help="With --reclassify, skip the newest N messages (page past the 1000 cap)",
    )
    direct_group = classify_parser.add_mutually_exclusive_group()
    direct_group.add_argument(
        "--text",
        type=str,
        default=None,
        help="Classify this email body text directly (prints JSON; no database needed)",
    )
    direct_group.add_argument(
        "--file",
        type=str,
        default=None,
        help="Classify a local .eml file directly (prints JSON; no database needed)",
    )
    direct_group.add_argument(
        "--stdin",
        action="store_true",
        help="Read email body text from stdin and classify it (prints JSON; no database needed)",
    )
    classify_parser.add_argument(
        "--subject",
        type=str,
        default=None,
        help="Email subject for direct classification",
    )
    classify_parser.add_argument(
        "--sender",
        type=str,
        default=None,
        help="Email sender for direct classification",
    )
    classify_parser.add_argument(
        "--filename",
        type=str,
        default=None,
        help="Attachment filename for direct classification",
    )
    classify_parser.add_argument(
        "--model-endpoint",
        type=str,
        default=None,
        help="Override model endpoint from config",
    )

    # export
    export_parser = subparsers.add_parser("export", help="Export reviewed label choices")
    export_parser.add_argument(
        "--format",
        choices=["csv", "json"],
        default="csv",
        help="Export format (default: csv)",
    )
    export_parser.add_argument(
        "--output",
        default=None,
        help="Output file path (default: print to stdout)",
    )
    export_parser.add_argument(
        "--include-skipped",
        action="store_true",
        help="Include messages that were explicitly skipped",
    )

    # suggest-labels
    suggest = subparsers.add_parser(
        "suggest-labels",
        help="Propose a personal label list from cached mail (local model only; never writes Gmail)",
    )
    suggest.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="How many cached messages to read (default 1000, max 1000)",
    )
    suggest.add_argument(
        "--batch-size",
        type=int,
        default=8,
        help="Full-body messages per local-model call (default 8)",
    )
    suggest.add_argument(
        "--output",
        default=None,
        help="Write this JSON path (default: timestamped file in the data dir)",
    )

    pilot = subparsers.add_parser(
        "pilot-report",
        help="Step 5 metrics from human review; local model commentary; flag label conflicts",
    )
    pilot.add_argument(
        "--output",
        default=None,
        help="Markdown path (default: data dir pilot-report.md)",
    )
    pilot.add_argument(
        "--holdout",
        type=float,
        default=0.2,
        help="Held-out fraction of reviewed messages (default 0.2)",
    )
    pilot.add_argument(
        "--no-model",
        action="store_true",
        help="Skip local-model commentary (numbers only)",
    )
    pilot.add_argument(
        "--latest",
        action="store_true",
        help="Score the newest proposal per message instead of the reviewed basis "
        "(use after reclassifying to evaluate a changed model or prompt)",
    )

    subparsers.add_parser(
        "sync-review",
        help="Export reviews, then either stop for conflict picks in the UI or refresh config.json examples",
    )

    apply_labels = subparsers.add_parser(
        "apply-labels",
        help="Optional Gmail sync: apply reviewed labels to Gmail (dry run unless --apply)",
    )
    apply_labels.add_argument(
        "--apply",
        action="store_true",
        help="Actually create Gmail labels and modify messages (default is a dry run)",
    )
    apply_labels.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum reviewed messages to process",
    )
    apply_labels.add_argument(
        "--message-id",
        default=None,
        help="Only this local message id (account_id:gmail_message_id)",
    )

    subparsers.add_parser(
        "sync-labels",
        help="Optional Gmail sync: read Gmail labels back for applied mail and record your edits as decisions",
    )

    return parser


def init_db(args, config):
    """Initialize the database.

    Args:
        args: Parsed arguments
        config: Configuration object
    """
    from Mailroom import db

    try:
        db_obj = db.DB(config.database_path)
        print(f"Database initialized successfully at {config.database_path}.")
        db_obj.close()
        return 0
    except Exception as e:
        print(f"Error initializing database: {e}", file=sys.stderr)
        return 1


def ingest_cmd(args, config) -> int:
    """Ingest local .eml files or directory into SQLite."""
    from Mailroom import db, ingest

    path = Path(args.path)
    if not path.exists():
        print(f"Error: Path does not exist: {args.path}", file=sys.stderr)
        return 1

    db_obj = db.DB(config.database_path)
    try:
        result = ingest.ingest_path(
            db_obj=db_obj,
            path=path,
            account_id=args.account,
            recursive=args.recursive,
            limit=args.limit,
        )
        acc = result.account_id
        print(
            f"Ingested {result.ingested}/{result.total_found} message(s) "
            f"into account '{acc}' (failures: {result.failed})."
        )
        return 0 if result.failed == 0 else 1
    except Exception as e:
        print(f"Error during ingestion: {e}", file=sys.stderr)
        return 1
    finally:
        db_obj.close()


def fix_schema(args, config):
    """Fix database schema.

    Args:
        args: Parsed arguments
        config: Configuration object
    """
    from Mailroom import db

    try:
        db_obj = db.DB(config.database_path)
        print(f"Database schema verified/updated at {config.database_path}.")
        db_obj.close()
        return 0
    except Exception as e:
        print(f"Error fixing schema: {e}", file=sys.stderr)
        return 1


def reset_db(args, config):
    """Reset database.

    Args:
        args: Parsed arguments
        config: Configuration object
    """
    from Mailroom import db

    db_path = Path(config.database_path)
    if db_path.exists():
        db_path.unlink()
        print(f"Database file removed: {db_path}")

    # Reinitialize
    db_obj = db.DB(config.database_path)
    db_obj.close()
    print("Database reset and reinitialized successfully.")
    return 0


def clear_cache(args, config):
    """Clear cached message text and proposals; keep accounts and decisions.

    Args:
        args: Parsed arguments
        config: Configuration object
    """
    from Mailroom import db

    try:
        db_obj = db.DB(config.database_path)
        db_obj.clear_cache()
        print(f"Cleared cached message text and proposals at {config.database_path}.")
        print("Accounts, decisions, subjects, Gmail IDs, and message metadata were kept.")
        print(
            "Subjects, review reasons, and exports can also be sensitive; "
            "clear-cache does not remove them."
        )
        db_obj.close()
        return 0
    except Exception as e:
        print(f"Error clearing cache: {e}", file=sys.stderr)
        return 1


def run_server(args, config):
    """Start the review server.

    Args:
        args: Parsed arguments
        config: Configuration object
    """
    from Mailroom import app

    print(f"Starting Mailroom review server on {args.host}:{args.port}")
    try:
        app.run_review_server(config, host=args.host, port=args.port)
        return 0
    except Exception as e:
        print(f"Server error: {e}", file=sys.stderr)
        return 1


def _is_model_installed(model_id: str, available_models: list) -> bool:
    """Check if model_id is present in available Ollama models, accounting for tags."""
    from Mailroom.classification import is_model_installed

    return is_model_installed(model_id, available_models)



def doctor_cmd(args, config) -> int:
    """Check environment health: loopback accessibility, backend availability, and database status."""
    import platform
    from pathlib import Path
    from Mailroom.config import is_loopback_url
    from Mailroom.classification import probe_model_endpoint, PROVIDER_OLLAMA

    strict = bool(getattr(args, "strict", False))
    has_issues = False
    has_warnings = False

    print("Mailroom Environment Doctor")
    print("=" * 60)

    # 1. Python & Core Environment
    py_version = platform.python_version()
    major, minor = sys.version_info[:2]
    if (major, minor) >= (3, 12):
        print(f"[✓] Python: {py_version} (supported >= 3.12)")
    else:
        print(f"[!] Python: {py_version} (WARNING: Mailroom requires Python >= 3.12)")
        has_issues = True

    # 2. Gmail Integration (Optional Dependencies)
    from Mailroom.gmail import check_gmail_dependencies
    gmail_ok, missing_gmail_deps = check_gmail_dependencies()
    if gmail_ok:
        print("[✓] Gmail dependencies: installed (keyring and Google OAuth libraries ready)")
    else:
        missing_str = ", ".join(missing_gmail_deps)
        print(f"[i] Gmail dependencies: not installed (optional, missing: {missing_str})")
        print("    Core local classification and review work offline.")
        print("    To enable Gmail sync, install: pip install 'mailroom[gmail]'")
        if strict:
            has_warnings = True

    # Check client credentials file (credentials.json)
    cred_file = config.config_dir / "credentials.json"
    if cred_file.exists():
        try:
            with open(cred_file, "r", encoding="utf-8") as f:
                cred_data = json.load(f)
            if isinstance(cred_data, dict) and "installed" in cred_data and isinstance(cred_data["installed"], dict):
                print(f"[✓] Client credentials: {cred_file.name} valid (desktop 'installed' client)")
            else:
                print(f"[!] Client credentials: '{cred_file.name}' is missing the 'installed' desktop client block.")
                print("    (Ensure OAuth credentials were created as a 'Desktop app' in Google Cloud Console)")
                has_warnings = True
        except Exception as e:
            print(f"[!] Client credentials: '{cred_file.name}' exists but contains invalid JSON: {e}")
            has_warnings = True
    else:
        print(f"[i] Client credentials: credentials.json not found in {config.config_dir} (optional for Gmail sync)")

    # Check stored OAuth token and scopes
    if gmail_ok:
        try:
            from Mailroom.gmail import _read_last_email, _load_credentials, GMAIL_MODIFY_SCOPE
            stored_email = _read_last_email(config)
            if stored_email:
                creds_dict = _load_credentials(stored_email)
                if creds_dict:
                    scopes = creds_dict.get("scopes") or []
                    if isinstance(scopes, str):
                        scopes = scopes.split()
                    if GMAIL_MODIFY_SCOPE in scopes:
                        print(f"[✓] Stored Gmail credentials: authorized with gmail.modify scope ({stored_email})")
                    else:
                        print("[!] Stored Gmail credentials lack gmail.modify scope. Run: python -m Mailroom auth --reauth")
                        has_warnings = True
                else:
                    print(f"[i] Stored Gmail credentials: no token found in keyring for {stored_email}")
            else:
                print("[i] Stored Gmail credentials: none stored (optional, run 'python -m Mailroom auth' to connect)")
        except Exception as e:
            print(f"[!] Stored Gmail credentials: error inspecting credentials: {e}")
            has_warnings = True

    # 3. Configuration
    config_path = Path(config.config_path)
    if config_path.exists():
        print(f"[✓] Configuration: {config_path} (valid)")
    else:
        print(f"[i] Configuration: {config_path} (will be auto-created with defaults on first run)")

    provider = config.model_provider or "auto"
    endpoint = config.model_endpoint
    model_id = config.model_id
    print(f"    Configured Provider: {provider} | Endpoint: {endpoint} | Model: {model_id}")

    # 4. Database Health
    db_path = Path(config.database_path)
    if db_path.exists():
        try:
            import sqlite3
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # Check schema_version table
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'")
            if not cursor.fetchone():
                current_ver = 0
            else:
                cursor.execute("SELECT MAX(version) as version FROM schema_version")
                row = cursor.fetchone()
                current_ver = row["version"] if row and row["version"] is not None else 0

            # Check messages count if table exists
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='messages'")
            msg_count = 0
            if cursor.fetchone():
                cursor.execute("SELECT COUNT(*) as c FROM messages")
                msg_count = cursor.fetchone()["c"]

            # Check decisions count if table exists
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='decisions'")
            dec_count = 0
            if cursor.fetchone():
                cursor.execute("SELECT COUNT(*) as c FROM decisions WHERE status IN ('accepted', 'corrected', 'skipped')")
                dec_count = cursor.fetchone()["c"]
            conn.close()

            from Mailroom.db import DB

            if current_ver >= DB.SCHEMA_VERSION:
                print(f"[✓] Database: {db_path} (Schema v{current_ver}, {msg_count} messages, {dec_count} reviewed decisions)")
            else:
                print(f"[!] Database: {db_path} (Schema v{current_ver} is outdated; run 'mailroom fix-schema')")
                has_issues = True
        except Exception as e:
            print(f"[!] Database: {db_path} (Error inspecting database: {e})")
            has_issues = True
    else:
        print(f"[i] Database: {db_path} (not created yet; will be auto-created on first review or classify)")

    # 5. Backend Connectivity & Model Presence
    if not is_loopback_url(endpoint):
        print(f"[!] Backend endpoint: '{endpoint}' is not a loopback URL!")
        print("    Mailroom requires loopback endpoints (127.0.0.1 or localhost) to prevent leaking email data.")
        has_issues = True
    else:
        timeout = min(getattr(config, "timeout", 30.0), 5.0)
        try:
            probe = probe_model_endpoint(endpoint, timeout=timeout, provider=config.model_provider)
            detected_prov = probe.get("provider", "unknown")
            active_model = probe.get("model_id", "unknown")
            print(f"[✓] Backend connectivity: {endpoint} reachable ({detected_prov})")

            if detected_prov == PROVIDER_OLLAMA:
                available = probe.get("available_models", [])
                model_matched = _is_model_installed(model_id, available)
                if model_matched:
                    print(f"[✓] Model presence: '{model_id}' found in Ollama local library")
                else:
                    print(f"[!] Model presence: '{model_id}' not found in Ollama local models.")
                    print(f"    [!] Model '{model_id}' is not installed locally. Run: ollama pull {model_id}")
                    if available:
                        print(f"    Available models in Ollama: {', '.join(available)}")
                    has_warnings = True
            else:
                if active_model == "unknown":
                    print("[!] Model status: no model currently active in backend")
                    has_warnings = True
                else:
                    print(f"[✓] Model status: active model '{active_model}'")
        except Exception as e:
            print(f"[!] Backend connectivity: unable to connect to {endpoint}")
            print(f"    Details: {e}")
            if provider == "ollama" or "11434" in endpoint:
                print("    -> Make sure Ollama is running: 'ollama serve'")
            elif provider == "tabby" or "8080" in endpoint:
                print("    -> Make sure TabbyAPI is running on port 8080")
            has_warnings = True

    print("=" * 60)
    if has_issues or (strict and has_warnings):
        print("Status: Issues detected. Please review recommendations above.")
        return 1
    elif has_warnings:
        print("Status: Functional with warnings (offline backend or missing optional extras).")
        return 0
    else:
        print("Status: All checks passed. System is ready.")
        return 0


def setup_cmd(args, config=None) -> int:
    """Interactive onboarding wizard for first-time Mailroom setup."""
    from pathlib import Path
    from Mailroom.config import (
        Config,
        default_app_dir,
        single_label_taxonomy,
        is_loopback_url,
    )
    from Mailroom.classification import probe_model_endpoint, PROVIDER_OLLAMA

    # Guard: Exit cleanly with instructions if stdin is not a TTY or --non-interactive is given
    if getattr(args, "non_interactive", False) or not sys.stdin.isatty():
        print("Mailroom setup wizard requires an interactive terminal (TTY).")
        print("For non-interactive setup, run:")
        print("  1. python -m Mailroom create-config [--profile standard|lightweight] [--taxonomy standard|single-label]")
        print("  2. python -m Mailroom init-db")
        print("  3. python -m Mailroom doctor")
        return 0

    print("=" * 60)
    print("Welcome to Mailroom Setup Wizard")
    print("=" * 60)
    print("This wizard will guide you through setting up Mailroom for local-first")
    print("mail classification, human review, and optional Gmail sync.\n")

    if getattr(args, "config", None):
        target_path = Path(args.config).resolve()
    elif config is not None:
        target_path = Path(config.config_path).resolve()
    else:
        target_path = default_app_dir() / "config.json"

    # Step 1: Configuration
    print("Step 1: Configuration")
    print("---------------------")
    create_or_overwrite = True
    profile = "standard"
    taxonomy = "standard"
    target_label = "Rechnungen"

    if target_path.exists():
        print(f"Configuration file already exists at {target_path}.")
        try:
            choice = input("Do you want to reconfigure and overwrite it? [y/N]: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\nSetup cancelled.")
            return 130
        if choice not in ("y", "yes"):
            create_or_overwrite = False
            print("Keeping existing configuration.\n")
            try:
                config = Config(str(target_path))
            except Exception as e:
                print(f"Error loading existing configuration: {e}", file=sys.stderr)
                return 1

    if create_or_overwrite:
        print("Select hardware profile for local inference:")
        print("  [1] standard     - Ollama qwen2.5:7b (recommended for >= 16GB RAM / 8GB VRAM) [default]")
        print("  [2] lightweight  - Ollama qwen2.5:3b (recommended for 8GB RAM / low VRAM)")
        try:
            prof_input = input("Enter choice [1/2, default: 1]: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\nSetup cancelled.")
            return 130

        if prof_input in ("2", "lightweight", "3b"):
            profile = "lightweight"
        else:
            profile = "standard"

        print("\nSelect label taxonomy archetype:")
        print("  [1] standard     - 14-label multi-axis taxonomy across Type, Purchase, Retention [default]")
        print("  [2] single-label - Focused single-target classification (e.g., invoices/receipts)")
        try:
            tax_input = input("Enter choice [1/2, default: 1]: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\nSetup cancelled.")
            return 130

        labels = None
        scan_query = None
        if tax_input in ("2", "single-label", "single"):
            taxonomy = "single-label"
            try:
                lbl_input = input("Enter target label name [default: Rechnungen]: ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\nSetup cancelled.")
                return 130
            if lbl_input:
                target_label = lbl_input
            labels = single_label_taxonomy(target_label)
            scan_query = Config.DOCUMENTS_ATTACHMENT_QUERY
        else:
            taxonomy = "standard"

        try:
            if target_path.exists():
                target_path.unlink()
            config = Config(str(target_path), profile=profile, labels=labels, scan_query=scan_query)
            print(f"[✓] Configuration created at {target_path} (profile: {profile}, taxonomy: {taxonomy})\n")
        except Exception as e:
            print(f"Error creating configuration: {e}", file=sys.stderr)
            return 1

    # Step 2: Database Initialization
    print("Step 2: Database")
    print("----------------")
    db_path = Path(config.database_path)
    if db_path.exists():
        print(f"[✓] Database already exists at {db_path}.\n")
    else:
        print(f"Initializing database at {db_path}...")
        try:
            from Mailroom.db import DB
            db_obj = DB(config.database_path)
            db_obj.close()
            print(f"[✓] Database initialized successfully at {db_path}.\n")
        except Exception as e:
            print(f"[!] Error initializing database: {e}\n", file=sys.stderr)

    # Step 3: Backend Diagnostic
    print("Step 3: Backend Diagnostic")
    print("--------------------------")
    endpoint = config.model_endpoint
    model_id = config.model_id
    prov = config.model_provider or "auto"
    print(f"Checking backend connectivity at {endpoint}...")

    if not is_loopback_url(endpoint):
        print(f"[!] Warning: Configured model endpoint '{endpoint}' is not a loopback URL!\n")
    else:
        try:
            probe = probe_model_endpoint(endpoint, timeout=5.0, provider=prov)
            detected_prov = probe.get("provider", "unknown")
            if detected_prov == PROVIDER_OLLAMA:
                available = probe.get("available_models", [])
                model_matched = _is_model_installed(model_id, available)
                if model_matched:
                    print(f"[✓] Backend reachable and model '{model_id}' is installed locally in Ollama.\n")
                else:
                    print(f"[!] Model '{model_id}' is not installed locally.")
                    if available:
                        print(f"    Available models in Ollama: {', '.join(available)}")
                    print(f"    Run: ollama pull {model_id}\n")
            else:
                print(f"[✓] Backend reachable ({detected_prov}, active model: {probe.get('model_id')}).\n")
        except Exception as e:
            print(f"[!] Backend connectivity: unable to connect to {endpoint}")
            print(f"    Details: {e}")
            if prov == "ollama" or "11434" in endpoint:
                print("    -> Make sure Ollama is running: 'ollama serve'")
                print(f"    -> Run 'ollama pull {model_id}' to download the model.\n")
            elif prov == "tabby" or "8080" in endpoint:
                print("    -> Make sure TabbyAPI is running on port 8080\n")

    # Step 4: Gmail (Optional)
    print("Step 4: Gmail Integration (Optional)")
    print("------------------------------------")
    from Mailroom.gmail import check_gmail_dependencies
    gmail_ok, missing_deps = check_gmail_dependencies()
    if not gmail_ok:
        missing_str = ", ".join(missing_deps)
        print(f"[i] Gmail dependencies not installed (optional, missing: {missing_str}).")
        print("    Core local mail classification works offline.")
        print("    To enable Gmail sync, install: pip install 'mailroom[gmail]'\n")
    else:
        from Mailroom.gmail import _read_last_email, _load_credentials
        stored_email = _read_last_email(config)
        stored_creds = _load_credentials(stored_email) if stored_email else None
        if stored_creds:
            print(f"[✓] Gmail already authenticated as: {stored_email}\n")
        else:
            cred_file = config.config_dir / "credentials.json"
            if cred_file.exists():
                print(f"Found OAuth client credentials at {cred_file}.")
                try:
                    auth_choice = input("Would you like to authenticate with Gmail now? [y/N]: ").strip().lower()
                except (KeyboardInterrupt, EOFError):
                    print("\nSetup cancelled.")
                    return 130
                if auth_choice in ("y", "yes"):
                    try:
                        from Mailroom.gmail import authenticate
                        authenticate(config)
                        print("[✓] Gmail authentication completed successfully.\n")
                    except Exception as e:
                        print(f"[!] Gmail authentication failed: {e}\n", file=sys.stderr)
                else:
                    print("Skipping Gmail authentication for now.\n")
            else:
                print("[i] Gmail sync is optional. No credentials.json found.")
                print(f"    To set up Gmail later, place your OAuth client JSON at: {cred_file}")
                print("    and run: python -m Mailroom auth\n")

    # Step 5: Completion
    print("Step 5: Completion")
    print("------------------")
    print("=" * 60)
    print("Mailroom Setup Complete!")
    print("=" * 60)
    print(f"Configuration: {config.config_path}")
    print(f"Database:      {config.database_path}")
    print(f"Backend:       {config.model_endpoint} ({config.model_id})")
    print("\nNext steps (local-first workflow):")
    print("  1. Ingest email:      python -m Mailroom ingest Mailroom/fixtures")
    print("  2. Classify messages: python -m Mailroom classify --limit 10")
    print("  3. Review in UI:      python -m Mailroom review")
    print("  4. Export choices:    python -m Mailroom export --format csv")
    print("\nOptional Gmail sync:")
    print("  - Scan inbox:         python -m Mailroom scan")
    print("  - Apply labels:       python -m Mailroom apply-labels --apply\n")
    return 0


def create_config_cmd(args):
    """Create initial configuration file.

    Args:
        args: Parsed arguments
    """
    from Mailroom.config import (
        Config,
        ConfigError,
        default_app_dir,
        single_label_taxonomy,
        load_template_file,
    )

    template = getattr(args, "template", None)
    taxonomy = getattr(args, "taxonomy", "standard")
    target_label = getattr(args, "target_label", None)

    # Validate flag combinations and constraints before touching filesystem
    if template:
        if taxonomy == "single-label":
            print(
                "Error: --template cannot be combined with --taxonomy single-label (mutually exclusive)",
                file=sys.stderr,
            )
            return 1
        if target_label is not None:
            print(
                "Error: --template cannot be combined with --target-label (mutually exclusive)",
                file=sys.stderr,
            )
            return 1
    elif target_label is not None and taxonomy != "single-label":
        print(
            "Error: --target-label requires --taxonomy single-label",
            file=sys.stderr,
        )
        return 1

    # Resolve label definitions
    labels = None
    if template:
        try:
            labels = load_template_file(template)
        except ConfigError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
    elif taxonomy == "single-label":
        try:
            labels = single_label_taxonomy(target_label)
        except ConfigError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    if args.config:
        target_path = Path(args.config).resolve()
    else:
        target_path = default_app_dir() / "config.json"

    if target_path.exists():
        print(f"Configuration file already exists: {target_path}")
        print("Edit it to customize settings.")
        return 0

    scan_query = None
    if taxonomy == "single-label":
        scan_query = Config.DOCUMENTS_ATTACHMENT_QUERY

    profile = getattr(args, "profile", "standard")
    try:
        Config(str(target_path), profile=profile, labels=labels, scan_query=scan_query)
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    print(f"Configuration created: {target_path} (profile: {profile})")
    profile_info = Config.HARDWARE_PROFILES.get(profile, Config.HARDWARE_PROFILES["standard"])
    print(
        f"Selected backend ({profile} profile): {profile_info['provider']} "
        f"{profile_info['id']} on {profile_info['endpoint']} "
        "(verify with: python -m Mailroom doctor)."
    )
    if profile != "tabby":
        print("For TabbyAPI use --profile tabby.")
    print("\nNext steps (local-first; no Gmail account needed):")
    print("1. Run: python -m Mailroom init-db")
    print("2. Run: python -m Mailroom ingest <path to .eml file or folder>")
    print("3. Run: python -m Mailroom classify --limit 10")
    print("4. Run: python -m Mailroom review   (http://127.0.0.1:5000)")
    print("5. Run: python -m Mailroom export --format csv --output choices.csv")
    print("\nOptional: mirror labels in Gmail")
    print("- Save a Desktop OAuth client JSON as credentials.json in this directory")
    print("- Run: python -m Mailroom auth")
    print("- Run: python -m Mailroom scan")
    print("- Run: python -m Mailroom apply-labels --apply   (dry run without --apply)")
    return 0


def auth_cmd(args, config):
    """Authenticate with Gmail (read-only)."""
    from Mailroom import gmail as gmail_module

    try:
        email = gmail_module.authenticate(config, reauth=bool(getattr(args, "reauth", False)))
        print(f"Gmail authentication complete for {email}.")
        print("Stored credentials are protected by the OS (DPAPI on Windows).")
        print("Run 'python -m Mailroom scan' to fetch a bounded inbox sample.")
        return 0
    except gmail_module.GmailError as e:
        print(f"Gmail auth error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Unexpected auth error: {e}", file=sys.stderr)
        return 1


class _InferenceWorkerError(Exception):
    """Raised when the scan inference worker is dead or stalled."""


def _run_inference_worker(target, error_holder, *args, **kwargs):
    """Run an inference worker and record unexpected death so scan can fail."""
    try:
        target(*args, **kwargs)
    except Exception as e:  # pragma: no cover - exercised via the scan tests
        error_holder["message"] = str(e) or e.__class__.__name__


def _suggest_during_scan_worker(work, client, batch_size, result_holder, db_path):
    """Run taxonomy batches as mail arrives. Never logs bodies."""
    from Mailroom import db as db_module
    from Mailroom import taxonomy as taxonomy_module

    def _progress(done, n_labels):
        print(f"taxonomy batch {done} ({n_labels} draft labels)", flush=True)

    stream = taxonomy_module.TaxonomyStream(client, batch_size=batch_size, on_batch=_progress)
    db_obj = db_module.DB(db_path)
    try:
        while True:
            item = work.get()
            if item is None:
                break
            account_id, _local_id, gmail_id = item
            row = db_obj.get_message_by_gmail_id(account_id, gmail_id)
            if row:
                stream.add(row)
    finally:
        db_obj.close()
    try:
        result_holder["result"] = stream.finish()
    except Exception as e:
        print(f"Taxonomy worker failed ({e}); writing whatever drafts exist.", flush=True)
        result_holder["result"] = {
            "labels": taxonomy_module.fallback_merge_drafts(stream.drafts),
            "message_count": stream.message_count,
            "batch_count": stream.batch_count,
            "failed_batches": stream.failed_batches + 1,
        }


def _classify_during_scan_worker(work, db_path, config, model_endpoint, stats):
    """Classify queued scan messages on a worker thread. Never logs bodies."""
    from Mailroom import classification as classify_module
    from Mailroom import db as db_module

    label_ids = [label["id"] for label in config.labels]
    labels = config.labels
    prompt_ver = classify_module.prompt_version(labels)
    label_ver = classify_module.label_definition_version(labels)
    db_obj = db_module.DB(db_path)
    client = classify_module.TabbyClient(
        endpoint=model_endpoint,
        timeout=config.timeout,
        model_id=config.model_id,
        provider=config.model_provider if model_endpoint == config.model_endpoint else None,
    )
    try:
        while True:
            item = work.get()
            if item is None:
                break
            account_id, message_id, gmail_id = item
            try:
                if db_obj.message_has_proposal(message_id):
                    stats["skipped"] += 1
                    print(f"Classify skip (already proposed): {gmail_id}", flush=True)
                    continue
                row = db_obj.get_message_by_gmail_id(account_id, gmail_id)
                body = (row or {}).get("body_preview") or ""
                if not body:
                    stats["failed"] += 1
                    print(f"Classify skip (no body): {gmail_id}", flush=True)
                    continue
                email_text = classify_module.build_mail_block(
                    sender=(row or {}).get("sender") or "",
                    sender_email=(row or {}).get("sender_email") or "",
                    subject=(row or {}).get("subject") or "",
                    body=body,
                )
                proposal = client.classify_message(email_text, label_ids, labels=labels)
                label_names = [
                    label["name"] for label in config.labels if label["id"] in proposal.label_ids
                ]
                db_obj.insert_proposal(
                    account_id=account_id,
                    message_id=message_id,
                    label_ids=proposal.label_ids,
                    label_names=label_names,
                    reason=proposal.reason,
                    source="model",
                    model_version=config.model_id,
                    prompt_version=prompt_ver,
                    label_definition_version=label_ver,
                    confidence=proposal.confidence,
                    abstain=proposal.abstain,
                )
                chosen = ", ".join(proposal.label_ids) if proposal.label_ids else "(none)"
                stats["ok"] += 1
                print(f"Classified {gmail_id}: {chosen}  abstain={proposal.abstain}", flush=True)
            except classify_module.ClassificationError as e:
                stats["failed"] += 1
                print(f"Classification error ({gmail_id}): {e}", file=sys.stderr, flush=True)
            except Exception as e:
                stats["failed"] += 1
                print(f"Classification error ({gmail_id}): {e}", file=sys.stderr, flush=True)
    finally:
        db_obj.close()


def scan_cmd(args, config):
    """Fetch a bounded Gmail sample, decode safely, and upsert into DB.

    Does not overwrite human decisions. Stores each message as it arrives.
    With --classify, local inference runs on a worker while Gmail download continues.
    """
    from datetime import datetime, timezone

    from Mailroom import gmail as gmail_module
    from Mailroom import db as db_module
    from Mailroom.config import is_loopback_url

    do_classify = bool(getattr(args, "classify", False))
    do_suggest = bool(getattr(args, "suggest_labels", False))
    model_endpoint = config.model_endpoint
    if do_classify and do_suggest:
        print("Choose either --classify or --suggest-labels, not both.", file=sys.stderr)
        return 1
    if do_classify:
        if not config.labels:
            print("scan --classify needs labels in config.json.", file=sys.stderr)
            return 1
        if not is_loopback_url(model_endpoint):
            print(
                "Model endpoint must be a loopback http(s) URL (email text stays local).",
                file=sys.stderr,
            )
            return 1
    if do_suggest:
        if not is_loopback_url(model_endpoint):
            print(
                "Model endpoint must be a loopback http(s) URL (email text stays local).",
                file=sys.stderr,
            )
            return 1

    try:
        service, email = gmail_module.get_gmail_service(config)
    except gmail_module.GmailError as e:
        print(f"Gmail error: {e}", file=sys.stderr)
        if "credentials" in str(e).lower():
            print("Run: python -m Mailroom auth", file=sys.stderr)
        print(
            "No Gmail? Ingest local .eml files instead: python -m Mailroom ingest <path>",
            file=sys.stderr,
        )
        return 1

    # Ensure account row
    db_obj = db_module.DB(config.database_path)
    worker = None
    work = None
    classify_stats = {"ok": 0, "failed": 0, "skipped": 0}
    inference_error = {"message": None}
    try:
        account_id = db_obj.get_or_create_account(email)
    except Exception as e:
        print(f"Failed to create account record: {e}", file=sys.stderr)
        db_obj.close()
        return 1

    expired = getattr(db_obj, "expired_body_count", 0) or 0
    if expired:
        print(f"Cleared {expired} expired body previews (older than {db_module.BODY_CACHE_EXPIRY_DAYS} days).")

    limit = args.limit if args.limit is not None else config.sample_limit
    limit = max(1, min(int(limit), gmail_module.SCAN_MAX_MESSAGES))
    per_month = getattr(args, "per_month", None)
    years = max(1, int(getattr(args, "years", 3) or 3))

    documents_only = bool(getattr(args, "documents_only", False))

    if per_month:
        per_month = max(1, min(int(per_month), gmail_module.SCAN_MAX_MESSAGES))
        windows = gmail_module.month_windows(years)
        base_query = gmail_module.resolve_scan_query(
            cli_query=args.query,
            documents_only=documents_only,
            default_query=gmail_module.MONTHLY_QUERY_BASE,
        )
        jobs = [
            (gmail_module.gmail_month_query(start, end, base_query), per_month, start.strftime("%Y-%m"))
            for start, end in windows
        ]
        query = base_query
        print(
            f"Scanning Gmail for account {email}: {per_month} message(s) × {len(jobs)} month(s) "
            f"({years} year(s), query='{base_query}') ..."
        )
    else:
        query = gmail_module.resolve_scan_query(
            config_query=config.scan_query,
            cli_query=args.query,
            documents_only=documents_only,
        )
        jobs = [(query, limit, "newest")]
        print(f"Scanning Gmail for account {email} (query='{query}', limit={limit}) ...")
    if do_classify:
        print(f"Classifying each download via {model_endpoint} while fetch continues.")
    if do_suggest:
        print(f"Suggesting labels via {model_endpoint} while fetch continues. Nothing is written to Gmail.")
    cached_ids = db_obj.list_cached_gmail_ids(account_id)
    if cached_ids:
        print(f"Already have {len(cached_ids)} message(s) with body text; those will not be re-downloaded.")

    suggest_holder = {}
    if do_classify:
        work = queue.Queue(maxsize=8)
        worker = threading.Thread(
            target=_run_inference_worker,
            args=(
                _classify_during_scan_worker,
                inference_error,
                work,
                config.database_path,
                config,
                model_endpoint,
                classify_stats,
            ),
            name="mailroom-classify",
            daemon=True,
        )
        worker.start()
    elif do_suggest:
        from Mailroom.classification import TabbyClient

        work = queue.Queue(maxsize=8)
        batch_size = max(1, min(int(getattr(args, "batch_size", 8) or 8), 20))
        client = TabbyClient(
            endpoint=model_endpoint,
            timeout=config.timeout,
            model_id=config.model_id,
            provider=config.model_provider if model_endpoint == config.model_endpoint else None,
        )
        worker = threading.Thread(
            target=_run_inference_worker,
            args=(
                _suggest_during_scan_worker,
                inference_error,
                work,
                client,
                batch_size,
                suggest_holder,
                config.database_path,
            ),
            name="mailroom-suggest-labels",
            daemon=True,
        )
        worker.start()

    inserted = 0
    updated = 0
    stored_ids = set()
    db_lock = threading.Lock()

    def store_decoded(d):
        nonlocal inserted, updated
        if not d.get("fetched_at"):
            d["fetched_at"] = sample_time
        gmail_id = d.get("gmail_message_id")
        if not gmail_id or gmail_id in stored_ids:
            return
        try:
            with db_lock:
                prev = db_obj.get_message_by_gmail_id(account_id, gmail_id)
                local_id = db_obj.upsert_message(account_id, d)
            stored_ids.add(gmail_id)
            if prev:
                updated += 1
            else:
                inserted += 1
            if work is not None and (d.get("body_preview") or ""):
                _enqueue_inference(local_id, gmail_id)
        except _InferenceWorkerError:
            # Propagate so the scan stops instead of silently hanging.
            raise
        except Exception as e:
            print(f"Warning: failed to store message {gmail_id}: {e}", file=sys.stderr)

    def _enqueue_inference(local_id, gmail_id):
        if work is None:
            return
        # A dead worker would make a blocking put hang forever, so check first
        # and bound the wait. Callers turn the failure into a scan error.
        if inference_error["message"] is not None:
            raise _InferenceWorkerError(inference_error["message"])
        if worker is not None and not worker.is_alive():
            inference_error["message"] = "inference worker stopped unexpectedly"
            raise _InferenceWorkerError(inference_error["message"])
        try:
            # IDs only — the worker reloads the cached body from SQLite.
            work.put((account_id, local_id, gmail_id), timeout=5)
        except queue.Full:
            inference_error["message"] = "inference worker stalled (queue full)"
            raise _InferenceWorkerError(inference_error["message"])

    def enqueue_cached(gmail_id):
        with db_lock:
            row = db_obj.get_message_by_gmail_id(account_id, gmail_id)
        if not row or not (row.get("body_preview") or ""):
            return
        _enqueue_inference(row.get("id"), gmail_id)

    sample_time = datetime.now(timezone.utc).isoformat()
    label_catalog = []
    scan_error = None
    try:
        _profile, label_catalog = gmail_module.fetch_account_and_labels(service)
        for month_query, month_limit, month_label in jobs:
            print(f"  {month_label} (limit {month_limit}) ...", flush=True)
            try:
                decoded_list, sample_time = gmail_module.fetch_bounded_sample(
                    service,
                    query=month_query,
                    limit=month_limit,
                    skip_ids=cached_ids,
                    on_message=store_decoded,
                    on_skip=enqueue_cached if work is not None else None,
                )
                # Tests (and callers that omit on_message) may still return a list.
                for d in decoded_list:
                    store_decoded(d)
                del decoded_list
            except Exception as e:
                scan_error = e
                print(f"  {month_label} stopped: {e}", file=sys.stderr, flush=True)
    except gmail_module.GmailError as e:
        scan_error = e
    except Exception as e:
        scan_error = e

    if work is not None:
        if worker.is_alive():
            try:
                work.put(None, timeout=5)
            except queue.Full:
                inference_error["message"] = (
                    inference_error["message"] or "inference worker stalled (queue full)"
                )
        worker.join(timeout=10)
        if worker.is_alive():
            inference_error["message"] = (
                inference_error["message"] or "inference worker did not stop"
            )

    if scan_error is not None and not stored_ids:
        print(f"Scan failed: {scan_error}", file=sys.stderr)
        db_obj.close()
        return 1
    if scan_error is not None:
        print(f"Scan stopped early: {scan_error}", file=sys.stderr)
        print(f"Kept {len(stored_ids)} message(s) already stored.")

    try:
        db_obj.record_scan_metadata(account_id, query, sample_time, label_catalog)
    except Exception as e:
        print(f"Warning: failed to store scan metadata: {e}", file=sys.stderr)

    print(f"Scan complete at {sample_time}.")
    print(f"Query stored: {query}")
    print(f"Gmail labels (reference): {len(label_catalog)}")
    print(f"Messages processed: {len(stored_ids)} (inserted: {inserted}, updated: {updated}).")
    if do_classify:
        print(
            f"Classified {classify_stats['ok']} "
            f"(failed {classify_stats['failed']}, skipped {classify_stats['skipped']})."
        )
    if do_suggest:
        from Mailroom import taxonomy as taxonomy_module

        result = suggest_holder.get("result") or {
            "labels": [],
            "message_count": 0,
            "batch_count": 0,
            "failed_batches": 0,
        }
        report = {
            "model_id": config.model_id,
            "model_endpoint": config.model_endpoint,
            "message_count": result.get("message_count", 0),
            "batch_count": result.get("batch_count", 0),
            "failed_batches": result.get("failed_batches", 0),
            "batch_size": max(1, min(int(getattr(args, "batch_size", 8) or 8), 20)),
            "partial": scan_error is not None or inference_error["message"] is not None,
            "labels": result.get("labels") or [],
            "notes": (
                "Draft vocabulary only. Edit this file, then decide whether to use it "
                "as Mailroom config. Does not create or apply Gmail labels."
            ),
        }
        try:
            target, latest = taxonomy_module.write_suggestion_report(
                config.config_dir, report
            )
            print(f"Wrote {target}")
            print(f"Also updated {latest} (previous timestamped files are kept).")
            print(f"{'id':<28} {'axis':<10} {'retention':<12} name")
            for label in report["labels"]:
                print(
                    f"{label['id']:<28} {label['axis']:<10} {label['retention']:<12} {label['name']}"
                )
        except OSError as e:
            print(f"Error writing suggestion report: {e}", file=sys.stderr)
    print("Human decisions were preserved. Use 'clear-cache' to drop cached bodies/proposals only.")
    print("Subjects, reasons, and exports can also be sensitive and are not removed by clear-cache.")
    db_obj.close()
    if inference_error["message"]:
        print(f"Inference worker failed: {inference_error['message']}", file=sys.stderr)
        return 1
    if scan_error is not None:
        return 1
    if do_classify and classify_stats["failed"]:
        return 1
    return 0


def _direct_email_fields(args) -> dict[str, str]:
    """Return subject/sender/sender_email/body/filename for direct classification."""
    from Mailroom import ingest as ingest_module

    subject = getattr(args, "subject", None) or ""
    sender = getattr(args, "sender", None) or ""
    filename = getattr(args, "filename", None) or ""
    sender_email = getattr(args, "sender_email", None) or ""

    if getattr(args, "text", None) is not None:
        return {
            "subject": subject,
            "sender": sender,
            "sender_email": sender_email,
            "filename": filename,
            "body": args.text,
        }
    if getattr(args, "stdin", False):
        return {
            "subject": subject,
            "sender": sender,
            "sender_email": sender_email,
            "filename": filename,
            "body": sys.stdin.read(),
        }
    if getattr(args, "file", None):
        path = Path(args.file)
        if not path.is_file():
            raise FileNotFoundError(f"File not found: {path}")
        decoded = ingest_module.parse_eml_bytes(path.read_bytes())
        return {
            "subject": subject or (decoded.get("subject") or ""),
            "sender": sender or (decoded.get("sender") or ""),
            "sender_email": sender_email or (decoded.get("sender_email") or ""),
            "filename": filename,
            "body": decoded.get("body_preview") or "",
        }

    return {
        "subject": subject,
        "sender": sender,
        "sender_email": sender_email,
        "filename": filename,
        "body": "",
    }


def _classify_direct_cmd(args, config) -> int:
    """Classify direct input (text/file/stdin/metadata) and print one JSON object to stdout."""
    from Mailroom.classification import ClassificationError, proposal_payload
    from Mailroom.classifier import EmailClassifier

    try:
        fields = _direct_email_fields(args)
    except OSError as e:
        print(json.dumps({"error": f"Error reading input: {e}"}, ensure_ascii=False), file=sys.stderr)
        return 1

    try:
        classifier = EmailClassifier(
            config=config,
            model_endpoint=getattr(args, "model_endpoint", None),
        )
        proposal = classifier.classify(
            labels=config.labels,
            subject=fields["subject"],
            sender=fields["sender"],
            sender_email=fields["sender_email"],
            filename=fields.get("filename"),
            body=fields["body"],
        )
    except ClassificationError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 1

    print(json.dumps(proposal_payload(proposal, config.labels), ensure_ascii=False))
    return 0


def classify_cmd(args, config):
    """Classify a message using the model.

    Args:
        args: Parsed arguments
        config: Configuration object
    """
    from Mailroom import classification as classify_module
    from Mailroom import db as db_module
    from Mailroom.config import is_loopback_url

    direct_mode = (
        getattr(args, "text", None) is not None
        or bool(getattr(args, "file", None))
        or bool(getattr(args, "stdin", False))
        or getattr(args, "subject", None) is not None
        or getattr(args, "filename", None) is not None
        or getattr(args, "sender", None) is not None
    )
    db_mode = bool(getattr(args, "message_id", None)) or getattr(args, "limit", None) is not None

    if direct_mode and db_mode:
        print(
            json.dumps(
                {
                    "error": "Use either a direct source (--text/--file/--stdin) or cached mail "
                    "(--message-id/--limit), not both."
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    if direct_mode:
        return _classify_direct_cmd(args, config)

    if not db_mode:
        print(
            "Provide --message-id or --limit N for cached mail, or direct inputs "
            "(--text/--file/--stdin/--subject/--filename) to classify one message directly.",
            file=sys.stderr,
        )
        return 1

    if not getattr(args, "message_id", None) and not getattr(args, "limit", None):
        print("Provide --message-id or --limit N to classify cached mail.", file=sys.stderr)
        return 1

    model_endpoint = args.model_endpoint if args.model_endpoint else config.model_endpoint
    if args.model_endpoint and not is_loopback_url(args.model_endpoint):
        print(
            "Model endpoint override must be a loopback http(s) URL (no cloud fallback).",
            file=sys.stderr,
        )
        return 1
    if not is_loopback_url(model_endpoint):
        print(
            "Model endpoint must be a loopback http(s) URL (email text stays local).",
            file=sys.stderr,
        )
        return 1

    label_ids = [label["id"] for label in config.labels]
    labels = config.labels
    prompt_ver = classify_module.prompt_version(labels)
    label_ver = classify_module.label_definition_version(labels)
    db_obj = db_module.DB(config.database_path)
    try:
        if args.message_id:
            message = db_obj.get_message(args.message_id)
            if not message:
                try:
                    account_id, gmail_message_id = args.message_id.split(":", 1)
                    message = db_obj.get_message_by_gmail_id(account_id, gmail_message_id)
                except ValueError:
                    print(f"Invalid message ID format: {args.message_id}", file=sys.stderr)
                    print("Format should be: account_id:message_id", file=sys.stderr)
                    return 1
            if not message:
                print(f"Message not found: {args.message_id}", file=sys.stderr)
                return 1
            targets = [
                {
                    "id": message["id"],
                    "account_id": message["account_id"],
                    "gmail_message_id": message.get("gmail_message_id"),
                }
            ]
        else:
            cap = args.limit if args.limit is not None else 1000
            if getattr(args, "reclassify", False):
                targets = db_obj.list_messages_with_proposals(
                    cap, offset=int(getattr(args, "offset", 0) or 0)
                )
                if not targets:
                    print("No messages with existing proposals to reclassify.")
                    return 0
            else:
                targets = db_obj.list_messages_pending_classification(cap)
                if not targets:
                    print("No cached messages with body text are waiting for a proposal.")
                    return 0

        print(f"Model endpoint: {model_endpoint}")
        print(f"Using {len(label_ids)} labels")
        print(f"Classifying {len(targets)} message(s) locally...")

        ok = 0
        failed = 0
        for index, row in enumerate(targets, start=1):
            message_id = row["id"]
            account_id = row["account_id"]
            gmail_message_id = row.get("gmail_message_id")
            message = db_obj.get_message(message_id)
            if not message and gmail_message_id:
                message = db_obj.get_message_by_gmail_id(account_id, gmail_message_id)
            body = (message or {}).get("body_preview") or ""
            if not body:
                print(f"[{index}/{len(targets)}] skip (no body): {message_id}", file=sys.stderr)
                failed += 1
                continue
            message = message or {}
            email_text = classify_module.build_mail_block(
                sender=message.get("sender") or "",
                sender_email=message.get("sender_email") or "",
                subject=message.get("subject") or "",
                body=body,
            )
            print(f"[{index}/{len(targets)}] {message_id}")
            try:
                proposal = classify_module.classify_message(
                    email_text=email_text,
                    label_ids=label_ids,
                    model_endpoint=model_endpoint,
                    timeout=config.timeout,
                    model_id=config.model_id,
                    labels=labels,
                    provider=config.model_provider if not args.model_endpoint else None,
                )
                label_names = [
                    label["name"] for label in config.labels if label["id"] in proposal.label_ids
                ]
                db_obj.insert_proposal(
                    account_id=account_id,
                    message_id=message_id,
                    label_ids=proposal.label_ids,
                    label_names=label_names,
                    reason=proposal.reason,
                    source="model",
                    model_version=config.model_id,
                    prompt_version=prompt_ver,
                    label_definition_version=label_ver,
                    confidence=proposal.confidence,
                    abstain=proposal.abstain,
                )
                chosen = ", ".join(proposal.label_ids) if proposal.label_ids else "(none)"
                print(f"  Labels: {chosen}  abstain={proposal.abstain}")
                ok += 1
            except classify_module.ClassificationError as e:
                print(f"  Classification error: {e}", file=sys.stderr)
                failed += 1
            except Exception as e:
                print(f"  Error: {e}", file=sys.stderr)
                failed += 1

        print(f"Done. Stored {ok} proposal(s); {failed} failed or skipped.")
        return 0 if failed == 0 else 1
    finally:
        db_obj.close()


def export_cmd(args, config):
    """Export reviewed label choices as CSV or JSON.

    Args:
        args: Parsed arguments
        config: Configuration object
    """
    from Mailroom import db as db_module
    from Mailroom import export as export_module

    db_obj = db_module.DB(config.database_path)
    try:
        decisions = db_obj.list_decisions_for_export(include_skipped=bool(args.include_skipped))
    except Exception as e:
        print(f"Error reading decisions: {e}", file=sys.stderr)
        return 1
    finally:
        db_obj.close()

    label_names = {label["id"]: label["name"] for label in config.labels}
    records = export_module.build_export_records(decisions, label_names)

    if args.format == "json":
        content = export_module.records_to_json(records)
    else:
        content = export_module.records_to_csv(records)

    if args.output:
        target = Path(args.output)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as e:
            print(f"Error writing export: {e}", file=sys.stderr)
            return 1
        print(f"Exported {len(records)} decision(s) to {target} ({args.format}).")
    else:
        print(content, end="")

    if not args.include_skipped:
        print(
            "Exported accepted/corrected decisions only. Use --include-skipped to add skips.",
            file=sys.stderr,
        )
    return 0


def suggest_labels_cmd(args, config):
    """Propose a label vocabulary from cached mail using the local model.

    Repeatable: each run reads the current cache and writes a new timestamped
    report. Previous reports are kept. Gmail and config.json are not modified.
    """
    from Mailroom import db as db_module
    from Mailroom.classification import ClassificationError, TabbyClient
    from Mailroom.config import is_loopback_url
    from Mailroom import taxonomy as taxonomy_module

    if not is_loopback_url(config.model_endpoint):
        print(
            "Model endpoint must be a loopback http(s) URL (email text stays local).",
            file=sys.stderr,
        )
        return 1

    limit = max(1, min(int(args.limit), 1000))
    batch_size = max(1, min(int(args.batch_size), 20))
    client = TabbyClient(
        endpoint=config.model_endpoint,
        timeout=config.timeout,
        model_id=config.model_id,
        provider=config.model_provider,
    )

    def _progress(done, n_labels):
        print(f"  batch {done} ({n_labels} draft labels)")

    stream = taxonomy_module.TaxonomyStream(client, batch_size=batch_size, on_batch=_progress)
    db_obj = db_module.DB(config.database_path)
    try:
        offset = 0
        while offset < limit:
            chunk = db_obj.list_messages_for_taxonomy(min(batch_size, limit - offset), offset=offset)
            if not chunk:
                break
            for row in chunk:
                stream.add(row)
            offset += len(chunk)
            del chunk
        if stream.message_count == 0:
            print("No cached messages with body text. Run: python -m Mailroom scan --limit 1000")
            return 1
        print(
            f"Proposing labels from {stream.message_count} cached message(s) "
            f"(batch size {batch_size}) via {config.model_endpoint}"
        )
        print("Local model only. Nothing is written to Gmail.")
        try:
            result = stream.finish()
        except ClassificationError as e:
            print(f"Taxonomy error: {e}", file=sys.stderr)
            return 1
    finally:
        db_obj.close()

    report = {
        "model_id": config.model_id,
        "model_endpoint": config.model_endpoint,
        "message_count": result["message_count"],
        "batch_count": result["batch_count"],
        "batch_size": batch_size,
        "failed_batches": result.get("failed_batches", 0),
        "labels": result["labels"],
        "notes": (
            "Draft vocabulary only. Edit this file, then decide whether to use it "
            "as Mailroom config. Does not create or apply Gmail labels."
        ),
    }

    try:
        target, latest = taxonomy_module.write_suggestion_report(
            config.config_dir, report, output=args.output
        )
    except OSError as e:
        print(f"Error writing report: {e}", file=sys.stderr)
        return 1

    print(f"Wrote {target}")
    print(f"Also updated {latest} (previous timestamped files are kept).")
    print("")
    print(f"{'id':<28} {'axis':<10} {'retention':<12} name")
    for label in report["labels"]:
        print(
            f"{label['id']:<28} {label['axis']:<10} {label['retention']:<12} {label['name']}"
        )
    return 0


def pilot_report_cmd(args, config):
    """Compute Step 5 metrics from human decisions; optional local-model prose."""
    from Mailroom import db as db_module
    from Mailroom import pilot as pilot_module
    from Mailroom.classification import TabbyClient
    from Mailroom.config import is_loopback_url

    db_obj = db_module.DB(config.database_path)
    try:
        allowed = [lab["id"] for lab in config.labels]
        metrics = pilot_module.compute_pilot_metrics(
            db_obj,
            allowed,
            holdout_fraction=float(args.holdout),
            prefer_latest=bool(getattr(args, "latest", False)),
        )
    finally:
        db_obj.close()

    client = None
    if not getattr(args, "no_model", False):
        if not is_loopback_url(config.model_endpoint):
            print("Skipping model commentary: endpoint is not loopback.", file=sys.stderr)
        else:
            client = TabbyClient(
                endpoint=config.model_endpoint,
                timeout=config.timeout,
                model_id=config.model_id,
                provider=config.model_provider,
            )

    local_md = pilot_module.build_pilot_report(
        metrics, config.model_id, client=client, include_conflict_phrases=True
    )
    public_md = pilot_module.stats_to_markdown(
        metrics, config.model_id, include_conflict_phrases=False
    )

    data_dir = Path(config.config_dir)
    local_path = Path(args.output) if args.output else data_dir / "pilot-report.md"
    conflicts_path = data_dir / "pilot-conflicts.json"
    public_path = Path(__file__).resolve().parent.parent / "docs" / "pilot-report.md"
    try:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(local_md, encoding="utf-8")
        conflicts_path.write_text(
            json.dumps(metrics.get("conflicts") or {}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        public_path.parent.mkdir(parents=True, exist_ok=True)
        public_path.write_text(public_md, encoding="utf-8")
    except OSError as e:
        print(f"Error writing pilot report: {e}", file=sys.stderr)
        return 1

    c = metrics.get("conflicts") or {}
    print(f"Reviewed: {metrics['n_reviewed_total']}")
    print(f"Conflicts: {c.get('n_groups', 0)} groups / {c.get('n_messages', 0)} messages")
    print(f"Local report (with phrases): {local_path}")
    print(f"Conflicts JSON: {conflicts_path}")
    print(f"Tracked report (no phrases): {public_path}")
    return 0


def sync_review_cmd(args, config):
    """Export decisions, then refresh config examples or stop for UI conflict picks.

    Exit 0: no conflicts, config.json examples updated.
    Exit 2: conflicts remain — open the review UI and pick a set.
    """
    import shutil

    from Mailroom import db as db_module
    from Mailroom import export as export_module
    from Mailroom import pilot as pilot_module

    data_dir = Path(config.config_dir)
    db_obj = db_module.DB(config.database_path)
    try:
        decisions = db_obj.list_decisions_for_export(include_skipped=True)
        names = {lab["id"]: lab["name"] for lab in config.labels}
        records = export_module.build_export_records(decisions, names)
        export_path = data_dir / "mailroom-export-latest.json"
        export_path.write_text(export_module.records_to_json(records), encoding="utf-8")
        print(f"Exported {len(records)} decision(s) to {export_path}")

        rows = pilot_module.list_reviewed_pairs(db_obj)
        conflicts = pilot_module.find_label_conflicts(rows)
        conflicts_path = data_dir / "pilot-conflicts.json"
        conflicts_path.write_text(
            json.dumps(conflicts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        if conflicts["n_groups"]:
            print(
                f"Conflicts: {conflicts['n_groups']} group(s) / {conflicts['n_messages']} messages."
            )
            print("Open the review UI and pick one label set per group (do not edit the JSON):")
            print("  python -m Mailroom review")
            print("  http://127.0.0.1:5000")
            print("Then run: python -m Mailroom sync-review")
            return 2

        backup = Path(str(config.config_path) + ".bak-sync-review")
        shutil.copy2(config.config_path, backup)
        added = pilot_module.refresh_config_examples(config, db_obj)
        config.validate()
        print("No conflicts.")
        print(f"Updated examples in {config.config_path} ({added} new example(s)).")
        print(f"Backup: {backup}")
        return 0
    except OSError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        db_obj.close()


def _write_apply_plan_csv(path, planned, config) -> None:
    """Write the dry-run/apply plan (message ids and label names only; no bodies)."""
    import csv

    names = {lab["id"]: lab.get("name") or lab["id"] for lab in config.labels}
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["message_id", "gmail_message_id", "account_email", "labels"])
        for target, desired in planned:
            writer.writerow(
                [
                    target["message_id"],
                    target["gmail_message_id"],
                    target.get("account_email") or "",
                    "; ".join(names.get(label_id, label_id) for label_id in desired),
                ]
            )


def apply_labels_cmd(args, config):
    """Create and apply reviewed labels in Gmail. Dry-run unless --apply."""
    from Mailroom import db as db_module
    from Mailroom import gmail as gmail_module

    do_apply = bool(getattr(args, "apply", False))
    known = {lab["id"] for lab in config.labels}

    db_obj = db_module.DB(config.database_path)
    try:
        targets = db_obj.list_gmail_apply_targets()
        if getattr(args, "message_id", None):
            targets = [t for t in targets if t["message_id"] == args.message_id]
        if args.limit is not None:
            targets = targets[: max(0, int(args.limit))]
        snapshots = {row["message_id"]: row["label_ids"] for row in db_obj.list_applied_labels()}
    finally:
        db_obj.close()

    if not targets:
        print("No accepted/corrected decisions to apply.")
        return 0

    planned = []
    needed = set()
    for target in targets:
        desired = [label_id for label_id in target["label_ids"] if label_id in known]
        planned.append((target, desired))
        needed.update(desired)

    print(f"Reviewed messages to label: {len(planned)}")
    print(f"Distinct labels needed: {len(needed)}")

    csv_path = Path(config.config_dir) / "apply-labels-plan.csv"
    _write_apply_plan_csv(csv_path, planned, config)

    if not do_apply:
        print("DRY RUN — nothing was written to Gmail.")
        print(f"Plan: {csv_path}")
        print("Re-run with --apply to create labels and update messages.")
        return 0

    try:
        service, email = gmail_module.get_gmail_service(config)
    except gmail_module.GmailError as e:
        print(f"Gmail error: {e}", file=sys.stderr)
        return 1

    scopes = gmail_module.get_stored_scopes(config)
    if gmail_module.GMAIL_MODIFY_SCOPE not in scopes:
        print(
            "Stored credentials lack the gmail.modify scope. Re-run:\n"
            "  python -m Mailroom auth --reauth",
            file=sys.stderr,
        )
        return 1
    print(f"Authenticated as: {email}")

    label_map = gmail_module.list_user_labels(service)
    label_ids = {name: gmail_module.ensure_label(service, name, label_map) for name in sorted(needed)}
    print(f"Gmail labels ready: {len(label_ids)}")

    db_obj = db_module.DB(config.database_path)
    applied = failed = 0
    try:
        for target, desired in planned:
            gmail_id = target["gmail_message_id"]
            desired_ids = [label_ids[name] for name in desired]
            previous = snapshots.get(target["message_id"]) or []
            previous_ids = [label_ids[name] for name in previous if name in label_ids]
            add = [label_id for label_id in desired_ids if label_id not in previous_ids]
            remove = [label_id for label_id in previous_ids if label_id not in desired_ids]
            try:
                gmail_module.modify_message_labels(
                    service, gmail_id, add_label_ids=add, remove_label_ids=remove
                )
                db_obj.record_applied_labels(target["account_id"], target["message_id"], desired)
                applied += 1
            except gmail_module.GmailError as e:
                failed += 1
                print(f"Failed {gmail_id}: {e}", file=sys.stderr)
    finally:
        db_obj.close()

    print(f"Applied labels to {applied} message(s); {failed} failed.")
    return 0 if failed == 0 else 1


def sync_labels_cmd(args, config):
    """Read Gmail labels back for applied mail; record user edits as decisions.

    A message whose Gmail labels still match what Mailroom applied is left as
    accepted. Any difference (including removing every label) is recorded as a
    corrected decision, and the snapshot is updated so the next run is quiet.
    """
    from Mailroom import db as db_module
    from Mailroom import gmail as gmail_module

    known = {lab["id"] for lab in config.labels}
    try:
        service, email = gmail_module.get_gmail_service(config)
    except gmail_module.GmailError as e:
        print(f"Gmail error: {e}", file=sys.stderr)
        return 1
    print(f"Authenticated as: {email}")

    label_map = gmail_module.list_user_labels(service)
    id_to_name = {label_id: name for name, label_id in label_map.items()}

    db_obj = db_module.DB(config.database_path, allowed_labels=config.get_label_ids())
    unchanged = corrected = errors = 0
    try:
        for snap in db_obj.list_applied_labels():
            gmail_id = snap["gmail_message_id"]
            previous = snap["label_ids"]
            try:
                current_ids = gmail_module.fetch_message_label_ids(service, gmail_id)
            except gmail_module.GmailError as e:
                errors += 1
                print(f"Failed to read {gmail_id}: {e}", file=sys.stderr)
                continue
            current = [
                id_to_name[label_id]
                for label_id in current_ids
                if label_id in id_to_name and id_to_name[label_id] in known
            ]
            if sorted(current) == sorted(previous):
                unchanged += 1
                continue
            db_obj.save_decision(snap["account_id"], snap["message_id"], "corrected", current)
            db_obj.record_applied_labels(snap["account_id"], snap["message_id"], current)
            corrected += 1
            print(f"{gmail_id}: {previous or '(none)'} -> {current or '(none)'}")
    finally:
        db_obj.close()

    print(f"Unchanged: {unchanged}; corrected: {corrected}; read errors: {errors}.")
    return 0


def main():
    """Main entry point."""
    parser = create_cli_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Handle create-config and setup separately before validation
    if args.command == "create-config":
        sys.exit(create_config_cmd(args))
    if args.command == "setup":
        sys.exit(setup_cmd(args))

    # Load configuration
    from Mailroom.config import Config, ConfigError

    try:
        config = Config(args.config)
        config.validate()
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error loading configuration: {e}", file=sys.stderr)
        sys.exit(1)

    # Execute command
    commands = {
        "doctor": doctor_cmd,
        "auth": auth_cmd,
        "scan": scan_cmd,
        "ingest": ingest_cmd,
        "init-db": init_db,
        "fix-schema": fix_schema,
        "reset-db": reset_db,
        "clear-cache": clear_cache,
        "run-server": run_server,
        "review": run_server,
        "classify": classify_cmd,
        "export": export_cmd,
        "suggest-labels": suggest_labels_cmd,
        "pilot-report": pilot_report_cmd,
        "sync-review": sync_review_cmd,
        "apply-labels": apply_labels_cmd,
        "sync-labels": sync_labels_cmd,
    }

    if args.command in commands:
        exit_code = commands[args.command](args, config)
        sys.exit(exit_code)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
