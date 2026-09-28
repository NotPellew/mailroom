"""Database schema and initialization for Mailroom."""

import sqlite3
import json
import os
import logging
import uuid
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, List, Dict, Set, Iterable

# Body cache expiry default (days). Bodies older than this may be cleared while retaining IDs/decisions.
BODY_CACHE_EXPIRY_DAYS = 7

logger = logging.getLogger(__name__)


def _add_column_if_missing(cursor: sqlite3.Cursor, table: str, column_sql: str) -> None:
    """Add a column. Ignore only SQLite's duplicate-column error."""
    try:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column_sql}")
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e).lower():
            raise


class DatabaseError(Exception):
    """Raised when database operations fail."""
    pass


def _kind_set_expr(column: str) -> str:
    """SQL for the sorted Type/* ids of a JSON array column, minus overlays.

    Used to detect where the latest suggestion disagrees with the saved
    decision on the kind axis (the ambiguous Targeted/Newsletter boundary).
    Type/NeedsReply and Type/NeedsAction are overlays, so they are excluded.
    Returns a delimited string, or '' when the column is missing/invalid.
    """
    return (
        "COALESCE((SELECT group_concat(k.value, char(1)) FROM ("
        f"SELECT value FROM json_each(CASE WHEN json_valid({column}) THEN {column} ELSE '[]' END) "
        "WHERE value LIKE 'Type/%' "
        "AND value NOT IN ('Type/NeedsReply', 'Type/NeedsAction') ORDER BY value) k), '')"
    )


class DB:
    """SQLite database for Mailroom."""

    # Schema version
    SCHEMA_VERSION = 6

    # Table definitions
    TABLE_ACCOUNTS = """
    CREATE TABLE IF NOT EXISTS accounts (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        email TEXT NOT NULL,
        gmail_id TEXT UNIQUE,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        last_sync TIMESTAMP,
        last_query TEXT,
        gmail_label_catalog TEXT,
        is_active INTEGER NOT NULL DEFAULT 1
    )
    """

    TABLE_MESSAGES = """
    CREATE TABLE IF NOT EXISTS messages (
        id TEXT PRIMARY KEY,
        thread_id TEXT,
        account_id TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'gmail',
        gmail_message_id TEXT,
        subject TEXT,
        sender TEXT,
        sender_email TEXT,
        received_at TIMESTAMP,
        body_preview TEXT,
        gmail_labels TEXT,
        fetched_at TIMESTAMP,
        truncated INTEGER NOT NULL DEFAULT 0,
        unsupported_content INTEGER NOT NULL DEFAULT 0,
        has_attachments INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
    )
    """

    TABLE_PROPOSALS = """
    CREATE TABLE IF NOT EXISTS proposals (
        id TEXT PRIMARY KEY,
        message_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        label_ids TEXT NOT NULL,
        label_names TEXT,
        reason TEXT,
        confidence REAL,
        abstain INTEGER NOT NULL DEFAULT 0,
        source TEXT NOT NULL,
        model_version TEXT NOT NULL,
        prompt_version TEXT NOT NULL,
        label_definition_version TEXT NOT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE,
        FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
    )
    """

    TABLE_DECISIONS = """
    CREATE TABLE IF NOT EXISTS decisions (
        id TEXT PRIMARY KEY,
        message_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending', 'accepted', 'corrected', 'skipped')),
        label_ids TEXT NOT NULL,
        proposal_id TEXT,
        user_notes TEXT,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE,
        FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE,
        UNIQUE (account_id, message_id)
    )
    """

    TABLE_APPLIED_LABELS = """
    CREATE TABLE IF NOT EXISTS applied_labels (
        message_id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        label_ids TEXT NOT NULL,
        applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE,
        FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
    )
    """

    INDEXES = [
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_account_gmail ON messages(account_id, gmail_message_id) WHERE gmail_message_id IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_messages_source ON messages(source)",
        "CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id)",
        "CREATE INDEX IF NOT EXISTS idx_proposals_message ON proposals(message_id)",
        "CREATE INDEX IF NOT EXISTS idx_proposals_account ON proposals(account_id)",
        "CREATE INDEX IF NOT EXISTS idx_decisions_message ON decisions(message_id)",
        "CREATE INDEX IF NOT EXISTS idx_decisions_account ON decisions(account_id)",
        "CREATE INDEX IF NOT EXISTS idx_decisions_status ON decisions(status)",
        "CREATE INDEX IF NOT EXISTS idx_applied_labels_account ON applied_labels(account_id)",
    ]

    def __init__(
        self,
        db_path: Optional[str] = None,
        allowed_labels: Optional[Iterable[str]] = None,
    ):
        """Initialize database.

        Args:
            db_path: Path to database file (defaults to the per-user app dir)
            allowed_labels: Optional collection of allowed label IDs for decision validation
        """
        if db_path is None:
            from Mailroom.config import default_app_dir

            db_path = str(default_app_dir() / "Mailroom.db")
        else:
            db_path = os.path.abspath(db_path)

        self.db_path = Path(db_path)
        self.db_dir = self.db_path.parent
        self.allowed_labels: Optional[Set[str]] = (
            set(allowed_labels) if allowed_labels is not None else None
        )

        # Ensure database directory exists
        self.db_dir.mkdir(parents=True, exist_ok=True)

        # Connect to database. A longer busy timeout avoids spurious "database
        # is locked" errors when the review page and a scan/classify worker
        # touch the same local file; WAL allows readers during a writer.
        self.conn = sqlite3.connect(
            self.db_path, check_same_thread=False, timeout=30.0
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        try:
            self.conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.DatabaseError as e:
            logger.warning("Could not enable WAL journal mode: %s", e)
        # Negative cache_size is KiB. Keep the page cache small so mail bodies
        # stay on disk instead of filling RAM.
        self.conn.execute("PRAGMA cache_size = -4096")
        self.conn.execute("PRAGMA mmap_size = 0")
        self.expired_body_count = 0

        # Initialize schema
        self._initialize()

    def _initialize(self):
        """Initialize database schema."""
        cursor = self.conn.cursor()

        # Create schema version table
        cursor.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY
            );
            """
        )

        # Check current version
        cursor.execute("SELECT MAX(version) as version FROM schema_version")
        result = cursor.fetchone()
        current_version = result["version"] if result and result["version"] is not None else 0

        logger.debug("Current schema version: %s, target: %s", current_version, self.SCHEMA_VERSION)

        if current_version < self.SCHEMA_VERSION:
            self._apply_migrations(cursor, current_version)
            self._create_indexes(cursor)
            self.conn.commit()
            self.conn.execute("PRAGMA foreign_keys = ON")
            fk_errors = self.conn.execute("PRAGMA foreign_key_check").fetchall()
            if fk_errors:
                logger.warning("Foreign key integrity check failed after migrations: %s", fk_errors)

        try:
            self.expired_body_count = self.enforce_body_cache_expiry()
        except Exception as e:
            logger.warning("Body cache expiry check failed: %s", e)
            self.expired_body_count = 0

    def set_allowed_labels(self, labels: Optional[Iterable[str]]) -> None:
        """Update allowed label IDs for decision validation."""
        self.allowed_labels = set(labels) if labels is not None else None

    def _apply_migrations(self, cursor: sqlite3.Cursor, from_version: int):
        """Apply database migrations.

        Args:
            cursor: Database cursor
            from_version: Current schema version
        """
        logger.debug("Applying migrations from version %s to %s", from_version, self.SCHEMA_VERSION)

        if from_version == 0:
            cursor.executescript(self.TABLE_ACCOUNTS)
            cursor.executescript(self.TABLE_MESSAGES)
            cursor.executescript(self.TABLE_PROPOSALS)
            cursor.executescript(self.TABLE_DECISIONS)

        if from_version < 2:
            _add_column_if_missing(cursor, "messages", "gmail_labels TEXT")
            _add_column_if_missing(cursor, "messages", "fetched_at TIMESTAMP")
            _add_column_if_missing(cursor, "messages", "truncated INTEGER NOT NULL DEFAULT 0")
            _add_column_if_missing(cursor, "messages", "unsupported_content INTEGER NOT NULL DEFAULT 0")
            _add_column_if_missing(cursor, "messages", "has_attachments INTEGER NOT NULL DEFAULT 0")

        if from_version < 3:
            _add_column_if_missing(cursor, "accounts", "last_query TEXT")
            _add_column_if_missing(cursor, "accounts", "gmail_label_catalog TEXT")

        if from_version < 4:
            # Persist abstention so the review UI can refuse "Accept" on it.
            _add_column_if_missing(cursor, "proposals", "abstain INTEGER NOT NULL DEFAULT 0")
            # Track which proposal a decision was based on, so a later
            # reclassification can be shown separately from the saved choice.
            _add_column_if_missing(cursor, "decisions", "proposal_id TEXT")
            # Backfill existing decisions only when there is exactly one
            # proposal, so a pre-v4 choice is not falsely reported as older
            # than its own proposal. With multiple proposals the basis is
            # unknown, so leave it NULL and let the UI flag the mismatch.
            cursor.execute(
                """
                UPDATE decisions
                SET proposal_id = (
                    SELECT p.id FROM proposals p
                    WHERE p.message_id = decisions.message_id
                    ORDER BY p.rowid DESC LIMIT 1
                )
                WHERE proposal_id IS NULL
                  AND (
                    SELECT COUNT(*) FROM proposals p2
                    WHERE p2.message_id = decisions.message_id
                  ) = 1
                """
            )

        if from_version < 5:
            # Snapshot of the labels Mailroom last applied to a Gmail message,
            # so sync-labels can tell a user edit from an untouched suggestion.
            cursor.executescript(self.TABLE_APPLIED_LABELS)

        if from_version < 6:
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='messages'"
            )
            if cursor.fetchone():
                _add_column_if_missing(cursor, "messages", "source TEXT NOT NULL DEFAULT 'gmail'")
                cursor.execute("PRAGMA table_info(messages)")
                cols = cursor.fetchall()
                gmail_col = next((c for c in cols if c["name"] == "gmail_message_id"), None)
                if gmail_col and gmail_col["notnull"]:
                    cursor.execute("PRAGMA foreign_keys = OFF")
                    cursor.execute(
                        """
                        CREATE TABLE messages_migration_v6 (
                            id TEXT PRIMARY KEY,
                            thread_id TEXT,
                            account_id TEXT NOT NULL,
                            source TEXT NOT NULL DEFAULT 'gmail',
                            gmail_message_id TEXT,
                            subject TEXT,
                            sender TEXT,
                            sender_email TEXT,
                            received_at TIMESTAMP,
                            body_preview TEXT,
                            gmail_labels TEXT,
                            fetched_at TIMESTAMP,
                            truncated INTEGER NOT NULL DEFAULT 0,
                            unsupported_content INTEGER NOT NULL DEFAULT 0,
                            has_attachments INTEGER NOT NULL DEFAULT 0,
                            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
                        )
                        """
                    )
                    cursor.execute(
                        """
                        INSERT INTO messages_migration_v6 (
                            id, thread_id, account_id, source, gmail_message_id,
                            subject, sender, sender_email, received_at, body_preview,
                            gmail_labels, fetched_at, truncated, unsupported_content, has_attachments, created_at
                        )
                        SELECT
                            id, thread_id, account_id,
                            COALESCE(source, 'gmail'),
                            gmail_message_id,
                            subject, sender, sender_email, received_at, body_preview,
                            gmail_labels, fetched_at, truncated, unsupported_content, has_attachments,
                            COALESCE(created_at, CURRENT_TIMESTAMP)
                        FROM messages
                        """
                    )
                    cursor.execute("DROP TABLE messages")
                    cursor.execute("ALTER TABLE messages_migration_v6 RENAME TO messages")
                    cursor.execute("PRAGMA foreign_keys = ON")
                cursor.execute("DROP INDEX IF EXISTS idx_messages_account_gmail")

        cursor.execute("INSERT INTO schema_version (version) VALUES (?)", (self.SCHEMA_VERSION,))

    def _create_indexes(self, cursor: sqlite3.Cursor):
        """Create database indexes."""
        for stmt in self.INDEXES:
            try:
                cursor.execute(stmt)
            except sqlite3.OperationalError as e:
                logger.warning("Error creating index: %s", e)

    def clear_cache(self) -> None:
        """Remove cached message text and proposals; keep accounts and decisions."""
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM proposals")
        cursor.execute("UPDATE messages SET body_preview = NULL")
        self.conn.commit()

    def enforce_body_cache_expiry(self, days: int = BODY_CACHE_EXPIRY_DAYS) -> int:
        """Clear body_preview for cached Gmail messages fetched more than `days` ago.

        Local ingest bodies are stored text, not a Gmail cache, so they are
        kept. Returns number of rows updated. Keeps IDs, metadata, and decisions.
        """
        if days <= 0:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            UPDATE messages
            SET body_preview = NULL
            WHERE fetched_at IS NOT NULL AND fetched_at < ?
              AND COALESCE(source, 'gmail') = 'gmail'
            """,
            (cutoff,),
        )
        n = cursor.rowcount
        self.conn.commit()
        return n

    @staticmethod
    def _gmail_mix_error(existing: str, email: str) -> DatabaseError:
        return DatabaseError(
            f"Refusing to mix Gmail accounts. Already using {existing}; authenticated as {email}. "
            "Re-auth as the existing account, or run reset-db to switch."
        )

    def get_or_create_account(
        self,
        email: str,
        name: Optional[str] = None,
        is_local: bool = False,
    ) -> str:
        """Ensure an account row exists for the given email. Return its id.

        For Gmail accounts, uses the email address as stable id and refuses a
        second active Gmail account (no accidental mixing). For local mailboxes
        (is_local=True), allows registering/using the local account alongside
        or without Gmail accounts.
        """
        if not email or not str(email).strip():
            raise DatabaseError("Account email is required")
        email = str(email).strip()
        cursor = self.conn.cursor()
        cursor.execute("SELECT id, email, gmail_id FROM accounts WHERE is_active = 1")
        active = cursor.fetchall()
        for row in active:
            if (row["email"] or "").lower() == email.lower() or (row["id"] or "").lower() == email.lower():
                # A local ingest can create the row for a Gmail address before
                # Gmail auth sees it. Promote it so the single-account guard
                # below cannot be bypassed by authenticating as a second user.
                if not is_local and row["gmail_id"] is None:
                    other = next(
                        (r for r in active if r["id"] != row["id"] and r["gmail_id"] is not None),
                        None,
                    )
                    if other is not None:
                        raise self._gmail_mix_error(other["email"] or other["id"], email)
                    cursor.execute(
                        "UPDATE accounts SET gmail_id = ? WHERE id = ?",
                        (email, row["id"]),
                    )
                    self.conn.commit()
                return row["id"]
        if not is_local:
            active_gmail = [r for r in active if r["gmail_id"] is not None]
            if active_gmail:
                existing = active_gmail[0]["email"] or active_gmail[0]["id"]
                raise self._gmail_mix_error(existing, email)
        acc_id = email
        display = name or email
        gmail_id = None if is_local else email
        cursor.execute(
            """
            INSERT INTO accounts (id, name, email, gmail_id)
            VALUES (?, ?, ?, ?)
            """,
            (acc_id, display, email, gmail_id),
        )
        self.conn.commit()
        return acc_id

    def record_scan_metadata(
        self,
        account_id: str,
        query: str,
        sample_time: str,
        label_catalog: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Store the scan query, sample time, and Gmail label catalog on the account."""
        catalog_json = json.dumps(label_catalog or [], ensure_ascii=False)
        cursor = self.conn.cursor()
        cursor.execute(
            """
            UPDATE accounts
            SET last_query = ?, last_sync = ?, gmail_label_catalog = ?
            WHERE id = ?
            """,
            (query, sample_time, catalog_json, account_id),
        )
        self.conn.commit()

    def get_account(self, account_id: str) -> Optional[Dict[str, Any]]:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT id, name, email, gmail_id, last_sync, last_query, gmail_label_catalog, is_active
            FROM accounts
            WHERE id = ?
            """,
            (account_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def upsert_message(self, account_id: str, decoded: Dict[str, Any]) -> str:
        """Insert or update a message row from a decoded message dict.

        Never overwrites existing human decisions. Returns the local message id.
        Decoded dict should contain keys produced by gmail.decode_gmail_message
        or ingest.parse_eml_bytes.
        """
        if not account_id:
            raise DatabaseError("account_id is required")

        gmail_id = decoded.get("gmail_message_id")
        source = decoded.get("source") or ("gmail" if gmail_id else "local")

        if gmail_id:
            local_id = f"{account_id}:{gmail_id}"
        else:
            msg_id = decoded.get("message_id")
            if not msg_id:
                raise DatabaseError("message_id or gmail_message_id is required")
            # Namespace local ids so a hostile Message-ID header cannot collide
            # with (or impersonate) a Gmail primary key.
            local_id = f"{account_id}:local:{msg_id}"

        cursor = self.conn.cursor()

        # Check if message already exists
        if gmail_id:
            cursor.execute(
                "SELECT id, body_preview FROM messages WHERE account_id = ? AND gmail_message_id = ?",
                (account_id, gmail_id),
            )
        else:
            cursor.execute(
                "SELECT id, body_preview FROM messages WHERE id = ?",
                (local_id,),
            )
        existing = cursor.fetchone()

        # Preserve any existing body if the new one is empty (defensive)
        new_body = decoded.get("body_preview") or ""
        if existing and not new_body and existing["body_preview"]:
            new_body = existing["body_preview"]

        gmail_labels_json = json.dumps(decoded.get("gmail_labels") or [], ensure_ascii=False)
        fetched_at = decoded.get("fetched_at") or datetime.now(timezone.utc).isoformat()
        truncated = 1 if decoded.get("truncated") else 0
        unsupported = 1 if decoded.get("unsupported_content") else 0
        has_attach = 1 if decoded.get("has_attachments") else 0

        target_id = existing["id"] if existing else local_id

        if existing:
            cursor.execute(
                """
                UPDATE messages
                SET thread_id = COALESCE(?, thread_id),
                    subject = COALESCE(?, subject),
                    sender = COALESCE(?, sender),
                    sender_email = COALESCE(?, sender_email),
                    received_at = COALESCE(?, received_at),
                    body_preview = ?,
                    gmail_labels = ?,
                    fetched_at = ?,
                    truncated = ?,
                    unsupported_content = ?,
                    has_attachments = ?,
                    source = COALESCE(?, source)
                WHERE id = ?
                """,
                (
                    decoded.get("thread_id"),
                    decoded.get("subject"),
                    decoded.get("sender"),
                    decoded.get("sender_email"),
                    decoded.get("received_at"),
                    new_body,
                    gmail_labels_json,
                    fetched_at,
                    truncated,
                    unsupported,
                    has_attach,
                    source,
                    target_id,
                ),
            )
        else:
            cursor.execute(
                """
                INSERT INTO messages (
                    id, thread_id, account_id, source, gmail_message_id,
                    subject, sender, sender_email, received_at, body_preview,
                    gmail_labels, fetched_at, truncated, unsupported_content, has_attachments
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    local_id,
                    decoded.get("thread_id"),
                    account_id,
                    source,
                    gmail_id,
                    decoded.get("subject"),
                    decoded.get("sender"),
                    decoded.get("sender_email"),
                    decoded.get("received_at"),
                    new_body,
                    gmail_labels_json,
                    fetched_at,
                    truncated,
                    unsupported,
                    has_attach,
                ),
            )

        self.conn.commit()
        return target_id

    def get_message(self, message_id: str) -> Optional[Dict[str, Any]]:
        """Fetch message by primary key (id)."""
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT id, thread_id, account_id, source, gmail_message_id, subject, sender, sender_email,
                   received_at, body_preview, gmail_labels, fetched_at, truncated,
                   unsupported_content, has_attachments
            FROM messages
            WHERE id = ?
            """,
            (message_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_message_by_gmail_id(self, account_id: str, gmail_message_id: str) -> Optional[Dict[str, Any]]:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT id, thread_id, account_id, gmail_message_id, subject, sender, sender_email,
                   received_at, body_preview, gmail_labels, fetched_at, truncated,
                   unsupported_content, has_attachments
            FROM messages
            WHERE account_id = ? AND gmail_message_id = ?
            """,
            (account_id, gmail_message_id),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return dict(row)

    def list_cached_gmail_ids(self, account_id: str) -> set:
        """Gmail ids that already have cached body text (for scan resume)."""
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT gmail_message_id FROM messages
            WHERE account_id = ?
              AND gmail_message_id IS NOT NULL AND gmail_message_id != ''
              AND body_preview IS NOT NULL AND body_preview != ''
            """,
            (account_id,),
        )
        return {row["gmail_message_id"] for row in cursor.fetchall()}

    def message_has_proposal(self, message_id: str) -> bool:
        """True if this local message already has at least one proposal."""
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT 1 FROM proposals WHERE message_id = ? LIMIT 1",
            (message_id,),
        )
        return cursor.fetchone() is not None

    def list_messages_pending_classification(self, limit: int) -> List[Dict[str, Any]]:
        """Return messages that have body text and no proposal yet.

        Newest first. Used by batch ``classify --limit``.
        """
        cap = max(1, min(int(limit), 1000))
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT m.id, m.account_id, m.gmail_message_id, m.subject
            FROM messages m
            WHERE m.body_preview IS NOT NULL AND m.body_preview != ''
              AND NOT EXISTS (
                  SELECT 1 FROM proposals p WHERE p.message_id = m.id
              )
            ORDER BY m.received_at DESC, m.id
            LIMIT ?
            """,
            (cap,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def list_messages_with_proposals(self, limit: int, offset: int = 0) -> List[Dict[str, Any]]:
        """Messages that already have a proposal (for --reclassify)."""
        cap = max(1, min(int(limit), 1000))
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT m.id, m.account_id, m.gmail_message_id, m.subject
            FROM messages m
            WHERE EXISTS (SELECT 1 FROM proposals p WHERE p.message_id = m.id)
            ORDER BY m.received_at DESC, m.id
            LIMIT ? OFFSET ?
            """,
            (cap, max(0, int(offset))),
        )
        return [dict(row) for row in cursor.fetchall()]

    def list_messages_for_taxonomy(self, limit: int, offset: int = 0) -> List[Dict[str, Any]]:
        """Newest messages that have cached body text, for vocabulary discovery."""
        cap = max(1, min(int(limit), 1000))
        off = max(0, int(offset))
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT id, sender, sender_email, subject, body_preview
            FROM messages
            WHERE body_preview IS NOT NULL AND body_preview != ''
            ORDER BY received_at DESC, id
            LIMIT ? OFFSET ?
            """,
            (cap, off),
        )
        return [dict(row) for row in cursor.fetchall()]

    def insert_proposal(
        self,
        account_id: str,
        message_id: str,
        label_ids: List[str],
        reason: str,
        source: str,
        model_version: str,
        prompt_version: str,
        label_definition_version: str,
        label_names: Optional[List[str]] = None,
        confidence: Optional[float] = None,
        abstain: bool = False,
    ) -> str:
        """Insert a new proposal row, keeping prior proposals and decisions.

        Returns the generated proposal id. Never modifies decisions.
        """
        if not message_id:
            raise DatabaseError("message_id is required")
        proposal_id = f"{message_id}:{uuid.uuid4().hex[:8]}"
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO proposals (
                id, message_id, account_id, label_ids, label_names, reason,
                confidence, abstain, source, model_version, prompt_version,
                label_definition_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                proposal_id,
                message_id,
                account_id,
                json.dumps(label_ids or [], ensure_ascii=False),
                json.dumps(label_names or [], ensure_ascii=False),
                reason,
                confidence,
                1 if abstain else 0,
                source,
                model_version,
                prompt_version,
                label_definition_version,
            ),
        )
        self.conn.commit()
        return proposal_id

    def get_proposals_for_message(self, message_id: str) -> List[Dict[str, Any]]:
        """Get all proposals for a message.

        Args:
            message_id: Message ID in format "account_id:gmail_message_id"

        Returns:
            List of proposal dictionaries
        """
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT id, message_id, account_id, label_ids, label_names, reason,
                   confidence, abstain, source, model_version, prompt_version,
                   label_definition_version, created_at
            FROM proposals
            WHERE message_id = ?
            ORDER BY rowid DESC
            """,
            (message_id,),
        )
        proposals = [dict(row) for row in cursor.fetchall()]
        for proposal in proposals:
            proposal["label_ids_parsed"] = _parse_json_list(proposal.get("label_ids"))
            proposal["label_names_parsed"] = _parse_json_list(proposal.get("label_names"))
            proposal["abstain"] = bool(proposal.get("abstain"))
        return proposals

    def delete_proposals(self, message_id: str) -> int:
        """Delete all proposals for a message.

        Args:
            message_id: Message ID to delete proposals for

        Returns:
            Number of proposals deleted
        """
        cursor = self.conn.cursor()
        cursor.execute(
            """
            DELETE FROM proposals
            WHERE message_id = ?
            """,
            (message_id,),
        )
        deleted = cursor.rowcount
        self.conn.commit()
        return deleted

    # ------------------------------------------------------------------
    # Review decisions (Step 4)
    # ------------------------------------------------------------------

    REVIEW_STATUSES = ("pending", "accepted", "corrected", "skipped")

    def get_decision(self, message_id: str) -> Optional[Dict[str, Any]]:
        """Return the saved decision for a local message id, or None."""
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT id, message_id, account_id, status, label_ids, proposal_id,
                   user_notes, created_at, updated_at
            FROM decisions
            WHERE message_id = ?
            """,
            (message_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        decision = dict(row)
        decision["label_ids_parsed"] = _parse_json_list(decision.get("label_ids"))
        return decision

    def count_decisions_using_label(self, label_id: str) -> int:
        """Count saved decisions whose stored label ids reference ``label_id``.

        Stored ids are normalized through the legacy aliases so a decision taken
        before a rename still blocks removal of the current label.
        """
        from Mailroom.config import normalize_label_id

        target = normalize_label_id(label_id)
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT label_ids FROM decisions WHERE label_ids IS NOT NULL AND label_ids != ''"
        )
        count = 0
        for row in cursor.fetchall():
            ids = {
                normalize_label_id(stored)
                for stored in _parse_json_list(row["label_ids"])
                if stored
            }
            if target in ids:
                count += 1
        return count

    def save_decision(
        self,
        account_id: str,
        message_id: str,
        status: str,
        label_ids: Optional[List[str]] = None,
        user_notes: Optional[str] = None,
        proposal_id: Optional[str] = None,
        commit: bool = True,
        validate_labels: bool = True,
        allowed_labels: Optional[Iterable[str]] = None,
    ) -> str:
        """Create or update the human decision for a message, transactionally.

        The message must already exist. Re-saving updates the single row for
        ``(account_id, message_id)`` so a message never accumulates decisions.
        ``label_ids`` is stored as an explicit (possibly empty) list; an empty
        accepted/corrected selection is therefore distinct from a skip.
        Pass ``commit=False`` to group several saves in one caller-managed
        transaction.
        Pass ``validate_labels=False`` to bypass label catalog validation
        (for legacy or synthetic test fixtures).
        """
        if status not in self.REVIEW_STATUSES:
            raise DatabaseError(f"Invalid review status: {status!r}")
        if not message_id or not account_id:
            raise DatabaseError("account_id and message_id are required")

        labels = list(label_ids or [])
        if status in ("pending", "skipped") and labels:
            raise DatabaseError(f"Status '{status}' cannot carry selected labels")

        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT id FROM messages WHERE id = ? AND account_id = ?",
            (message_id, account_id),
        )
        if not cursor.fetchone():
            raise DatabaseError(f"Message not found: {message_id}")

        cursor.execute(
            "SELECT id, label_ids FROM proposals WHERE message_id = ? ORDER BY rowid DESC LIMIT 1",
            (message_id,),
        )
        proposal_row = cursor.fetchone()
        if proposal_id is None:
            proposal_id = proposal_row["id"] if proposal_row else None

        cursor.execute(
            "SELECT id, label_ids FROM decisions WHERE message_id = ?",
            (message_id,),
        )
        existing = cursor.fetchone()

        if validate_labels and labels:
            if allowed_labels is not None:
                effective_allowed = set(allowed_labels)
            elif self.allowed_labels is not None:
                effective_allowed = set(self.allowed_labels)
            else:
                from Mailroom.config import DEFAULT_LABEL_IDS

                effective_allowed = set(DEFAULT_LABEL_IDS)

            from Mailroom.config import LEGACY_LABEL_ALIASES

            permissible = set(effective_allowed)
            permissible.update(LEGACY_LABEL_ALIASES.keys())
            permissible.update(LEGACY_LABEL_ALIASES.values())

            if existing and existing["label_ids"]:
                permissible.update(_parse_json_list(existing["label_ids"]))
            if proposal_row and proposal_row["label_ids"]:
                permissible.update(_parse_json_list(proposal_row["label_ids"]))

            invalid = [lid for lid in labels if lid not in permissible]
            if invalid:
                raise DatabaseError(
                    f"Unknown or unconfigured label id(s): {sorted(invalid)}. "
                    f"Allowed labels: {sorted(effective_allowed)}"
                )

        payload = json.dumps(labels, ensure_ascii=False)

        try:
            if existing:
                cursor.execute(
                    """
                    UPDATE decisions
                    SET status = ?, label_ids = ?, user_notes = ?, proposal_id = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (status, payload, user_notes, proposal_id, existing["id"]),
                )
                decision_id = existing["id"]
            else:
                decision_id = f"{message_id}:{uuid.uuid4().hex[:8]}"
                cursor.execute(
                    """
                    INSERT INTO decisions (
                        id, message_id, account_id, status, label_ids, user_notes, proposal_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (decision_id, message_id, account_id, status, payload, user_notes, proposal_id),
                )
            if commit:
                self.conn.commit()
        except Exception:
            if commit:
                self.conn.rollback()
            raise
        return decision_id

    def latest_proposal_for_message(self, message_id: str) -> Optional[Dict[str, Any]]:
        """Return the most recently inserted proposal for a message, or None."""
        proposals = self.get_proposals_for_message(message_id)
        return proposals[0] if proposals else None

    # Shared FROM/JOIN for review queries. Filtering happens in SQL so a list
    # request never loads the whole table (pilot target: several thousand mail).
    _REVIEW_FROM = """
        FROM messages m
        LEFT JOIN accounts a ON a.id = m.account_id
        LEFT JOIN proposals p ON p.rowid = (
            SELECT p2.rowid FROM proposals p2
            WHERE p2.message_id = m.id
            ORDER BY p2.rowid DESC LIMIT 1
        )
        LEFT JOIN decisions d ON d.message_id = m.id
    """

    @staticmethod
    def _review_filter_clause(
        status: Optional[str] = None,
        label: Optional[str] = None,
        state: Optional[str] = None,
        account_id: Optional[str] = None,
        message_id: Optional[str] = None,
        disagreement: Optional[bool] = None,
        source: Optional[str] = None,
    ):
        """Build the shared WHERE clause and parameters for review queries."""
        conditions: List[str] = []
        params: List[Any] = []
        if message_id:
            conditions.append("m.id = ?")
            params.append(message_id)
        if account_id:
            conditions.append("m.account_id = ?")
            params.append(account_id)
        if source and source != "all":
            conditions.append("COALESCE(m.source, 'gmail') = ?")
            params.append(source)
        if status and status != "all":
            conditions.append("COALESCE(d.status, 'unreviewed') = ?")
            params.append(status)
        if state and state != "all":
            if state == "error":
                conditions.append("p.id IS NULL")
            elif state == "uncertain":
                conditions.append("p.id IS NOT NULL AND p.abstain = 1")
            elif state == "proposed":
                conditions.append("p.id IS NOT NULL AND COALESCE(p.abstain, 0) = 0")
            elif state == "scored":
                conditions.append("p.id IS NOT NULL")
            else:
                conditions.append("0")
        if label and label != "all":
            # json_valid() keeps a corrupt label_ids value from aborting the
            # whole query; such a row simply doesn't match the label filter.
            conditions.append(
                "EXISTS (SELECT 1 FROM json_each("
                "CASE WHEN json_valid(p.label_ids) THEN p.label_ids ELSE '[]' END"
                ") je WHERE je.value = ?)"
            )
            params.append(label)
        if disagreement:
            # Reviewed messages whose latest suggestion differs from the saved
            # choice on the kind axis (NeedsReply/NeedsAction are overlays, so they are ignored).
            conditions.append(
                "d.status IN ('accepted', 'corrected') AND p.id IS NOT NULL AND "
                f"{_kind_set_expr('p.label_ids')} <> {_kind_set_expr('d.label_ids')}"
            )
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        return where, params

    def list_review_items(
        self,
        status: Optional[str] = None,
        label: Optional[str] = None,
        state: Optional[str] = None,
        account_id: Optional[str] = None,
        message_id: Optional[str] = None,
        disagreement: Optional[bool] = None,
        include_body: bool = False,
        limit: Optional[int] = None,
        offset: int = 0,
        source: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List messages with their latest proposal and saved decision.

        Filters (all optional, ``None``/``"all"`` means no filter):
        - ``status``: unreviewed | pending | accepted | corrected | skipped
        - ``label``: a proposed label id
        - ``state``: proposed | uncertain | error (suggestion quality)
        - ``message_id``: restrict to one local message id
        - ``disagreement``: only reviewed messages whose latest suggestion's
          kind differs from the saved choice
        - ``source``: local | gmail

        ``include_body`` is off by default so list views never load cached
        bodies into memory; the detail view opts in. ``limit``/``offset``
        paginate the result (no limit returns every match).
        """
        cursor = self.conn.cursor()
        body_col = "m.body_preview," if include_body else "NULL AS body_preview,"
        where, params = self._review_filter_clause(
            status=status,
            label=label,
            state=state,
            account_id=account_id,
            message_id=message_id,
            disagreement=disagreement,
            source=source,
        )
        sql = f"""
            SELECT m.id AS message_id,
                   m.gmail_message_id,
                   m.source,
                   m.thread_id,
                   m.account_id,
                   m.subject,
                   m.sender,
                   m.sender_email,
                   m.received_at,
                   {body_col}
                   (m.body_preview IS NOT NULL AND m.body_preview != '') AS has_body,
                   m.truncated,
                   m.unsupported_content,
                   m.has_attachments,
                   a.email AS account_email,
                   p.id AS proposal_id,
                   p.label_ids AS proposal_label_ids,
                   p.label_names AS proposal_label_names,
                   p.reason AS proposal_reason,
                   p.abstain AS proposal_abstain,
                   p.source AS proposal_source,
                   p.created_at AS proposal_created_at,
                   d.status AS decision_status,
                   d.label_ids AS decision_label_ids,
                   d.proposal_id AS decision_proposal_id,
                   d.user_notes AS decision_notes,
                   d.updated_at AS decision_updated_at
            {self._REVIEW_FROM}
            {where}
            ORDER BY m.received_at DESC, m.id
        """
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params = list(params) + [int(limit), max(0, int(offset))]
        cursor.execute(sql, params)
        return [self._row_to_review_item(row) for row in cursor.fetchall()]

    def count_review_items(
        self,
        status: Optional[str] = None,
        label: Optional[str] = None,
        state: Optional[str] = None,
        account_id: Optional[str] = None,
        disagreement: Optional[bool] = None,
        source: Optional[str] = None,
    ) -> int:
        """Count messages matching the review filters (for pagination)."""
        where, params = self._review_filter_clause(
            status=status,
            label=label,
            state=state,
            account_id=account_id,
            disagreement=disagreement,
            source=source,
        )
        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) AS c " + self._REVIEW_FROM + where, params)
        return cursor.fetchone()["c"]

    def get_review_item(self, message_id: str) -> Optional[Dict[str, Any]]:
        """Return one review item, including its cached body text."""
        items = self.list_review_items(message_id=message_id, include_body=True)
        return items[0] if items else None

    def list_decisions_for_export(self, include_skipped: bool = False) -> List[Dict[str, Any]]:
        """Return reviewed decisions with message identity, for export.

        By default only accepted/corrected decisions are returned; pass
        ``include_skipped=True`` to also include explicitly skipped messages.
        Pending/unreviewed messages are never exported.
        """
        statuses = ["accepted", "corrected"]
        if include_skipped:
            statuses.append("skipped")
        placeholders = ",".join("?" for _ in statuses)
        cursor = self.conn.cursor()
        cursor.execute(
            f"""
            SELECT d.account_id,
                   d.message_id,
                   d.status,
                   d.label_ids,
                   d.updated_at AS reviewed_at,
                   COALESCE(m.gmail_message_id, m.id) AS gmail_message_id,
                   m.source,
                   m.thread_id
            FROM decisions d
            JOIN messages m ON m.id = d.message_id
            WHERE d.status IN ({placeholders})
            ORDER BY d.updated_at, d.message_id
            """,
            statuses,
        )
        rows = []
        for row in cursor.fetchall():
            record = dict(row)
            record["label_ids"] = _parse_json_list(record.get("label_ids"))
            rows.append(record)
        return rows

    def list_gmail_apply_targets(self) -> List[Dict[str, Any]]:
        """Accepted/corrected decisions joined to the Gmail ids needed to apply labels.

        Saved label ids are normalized through the legacy aliases so a decision
        taken before a rename is not silently dropped when building a plan.
        """
        from Mailroom.config import normalize_label_id

        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT d.message_id,
                   d.account_id,
                   d.status,
                   d.label_ids,
                   m.gmail_message_id,
                   m.subject,
                   m.sender,
                   a.email AS account_email
            FROM decisions d
            JOIN messages m ON m.id = d.message_id
            LEFT JOIN accounts a ON a.id = d.account_id
            WHERE d.status IN ('accepted', 'corrected')
              AND m.gmail_message_id IS NOT NULL
              AND m.gmail_message_id != ''
            ORDER BY m.gmail_message_id
            """
        )
        rows = []
        for row in cursor.fetchall():
            record = dict(row)
            ids = dict.fromkeys(
                normalize_label_id(label_id)
                for label_id in _parse_json_list(record.get("label_ids"))
                if label_id
            )
            record["label_ids"] = [label_id for label_id in ids if label_id]
            rows.append(record)
        return rows

    def record_applied_labels(self, account_id: str, message_id: str, label_ids: List[str]) -> None:
        """Store the labels Mailroom last applied to a message (upsert)."""
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO applied_labels (message_id, account_id, label_ids, applied_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(message_id) DO UPDATE SET
                label_ids = excluded.label_ids,
                account_id = excluded.account_id,
                applied_at = CURRENT_TIMESTAMP
            """,
            (message_id, account_id, serialize_labels(label_ids)),
        )
        self.conn.commit()

    def get_applied_labels(self, message_id: str) -> Optional[List[str]]:
        """Return the last applied label ids for a message, or None if never applied."""
        row = self.conn.execute(
            "SELECT label_ids FROM applied_labels WHERE message_id = ?", (message_id,)
        ).fetchone()
        if row is None:
            return None
        return _parse_json_list(row["label_ids"])

    def list_applied_labels(self) -> List[Dict[str, Any]]:
        """Return every applied snapshot joined to its message's Gmail id."""
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT al.message_id, al.account_id, al.label_ids, al.applied_at,
                   m.gmail_message_id
            FROM applied_labels al
            JOIN messages m ON m.id = al.message_id
            ORDER BY m.gmail_message_id
            """
        )
        rows = []
        for row in cursor.fetchall():
            record = dict(row)
            record["label_ids"] = _parse_json_list(record.get("label_ids"))
            rows.append(record)
        return rows

    @staticmethod
    def _row_to_review_item(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["proposal_label_ids"] = _parse_json_list(item.get("proposal_label_ids"))
        item["proposal_label_names"] = _parse_json_list(item.get("proposal_label_names"))
        item["decision_label_ids"] = _parse_json_list(item.get("decision_label_ids"))
        item["has_body"] = bool(item.get("has_body"))
        item["truncated"] = bool(item.get("truncated"))
        item["unsupported_content"] = bool(item.get("unsupported_content"))
        item["has_attachments"] = bool(item.get("has_attachments"))
        item["abstain"] = bool(item.get("proposal_abstain"))
        item["has_proposal"] = item.get("proposal_id") is not None
        item["review_status"] = item.get("decision_status") or "unreviewed"

        if not item["has_proposal"]:
            item["suggestion_state"] = "error"
        elif item["abstain"]:
            item["suggestion_state"] = "uncertain"
        else:
            item["suggestion_state"] = "proposed"
        return item

    def close(self):
        """Close database connection."""
        self.conn.close()

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()


def _parse_json_list(value: Any) -> List[str]:
    """Parse a stored JSON list into a list, returning [] on any problem."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def serialize_labels(labels: List[str]) -> str:
    """Serialize label list to JSON string.

    Args:
        labels: List of label IDs

    Returns:
        JSON string of labels
    """
    return json.dumps(labels, ensure_ascii=False)


def deserialize_labels(label_str: str) -> List[str]:
    """Deserialize label JSON string.

    Args:
        label_str: JSON string of labels

    Returns:
        List of label IDs

    Raises:
        DatabaseError: If parsing fails
    """
    try:
        return json.loads(label_str)
    except (json.JSONDecodeError, TypeError) as e:
        raise DatabaseError(f"Failed to deserialize labels: {e}")
