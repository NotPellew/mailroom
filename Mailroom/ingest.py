"""Local email ingestion for Mailroom (.eml files and offline sources).

Decouples ingestion from Gmail, allowing local emails and synthetic fixtures
to be parsed, stored, and reviewed without Google credentials.
"""

import email
import email.policy
import email.utils
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from Mailroom.db import DB
from Mailroom.gmail import BODY_PREVIEW_LIMIT, _html_to_text

logger = logging.getLogger(__name__)


@dataclass
class IngestResult:
    """Summary of an ingestion run."""

    total_found: int = 0
    ingested: int = 0
    failed: int = 0
    message_ids: List[str] = field(default_factory=list)
    account_id: str = "local"


def parse_eml_bytes(data: bytes, source: str = "local") -> Dict[str, Any]:
    """Parse raw RFC 822 / MIME bytes into normalized message fields.

    Extracts subject, sender, date, thread identifiers, attachments, and inert
    text preview (stripping HTML scripts/styles). Resolves message identity
    using RFC 822 Message-ID header if present, falling back to a deterministic
    SHA-256 content hash.
    """
    msg = email.message_from_bytes(data, policy=email.policy.default)

    subject = str(msg.get("subject") or "")
    from_header = str(msg.get("from") or "")
    display_name, addr = email.utils.parseaddr(from_header)
    sender = display_name or addr or from_header
    sender_email = addr or ""

    date_header = msg.get("date")
    received_at: Optional[str] = None
    if date_header:
        try:
            dt = email.utils.parsedate_to_datetime(str(date_header))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            received_at = dt.astimezone(timezone.utc).isoformat()
        except Exception:
            received_at = None

    thread_id: Optional[str] = None
    ref = msg.get("in-reply-to") or msg.get("references")
    if ref:
        thread_id = str(ref).strip().split()[0].strip("<>")

    msg_id_header = str(msg.get("message-id") or "").strip()
    cleaned_id = msg_id_header.strip("<> \t\r\n")
    if cleaned_id:
        message_id = cleaned_id
    else:
        content_hash = hashlib.sha256(data).hexdigest()[:24]
        message_id = f"eml_{content_hash}"

    plain_text = ""
    html_text = ""
    has_attachments = False

    def _inspect_part(part: Any) -> bool:
        """Record a leaf part's text. Returns False for attachment subtrees."""
        nonlocal plain_text, html_text, has_attachments
        content_disposition = (part.get_content_disposition() or "").lower()
        filename = part.get_filename() or ""
        if "attachment" in content_disposition or filename:
            has_attachments = True
            return False

        content_type = (part.get_content_type() or "").lower()
        if content_type == "text/plain" and not plain_text:
            try:
                plain_text = part.get_content()
            except Exception:
                raw_payload = part.get_payload(decode=True)
                if raw_payload:
                    plain_text = raw_payload.decode("utf-8", errors="replace")
        elif content_type == "text/html" and not html_text:
            try:
                raw_html = part.get_content()
            except Exception:
                raw_payload = part.get_payload(decode=True)
                raw_html = raw_payload.decode("utf-8", errors="replace") if raw_payload else ""
            if raw_html:
                html_text = _html_to_text(str(raw_html))
        return True

    def _walk_parts(part: Any) -> None:
        for child in part.iter_parts():
            if _inspect_part(child):
                _walk_parts(child)

    if msg.is_multipart():
        _walk_parts(msg)
    else:
        _inspect_part(msg)

    body = plain_text or html_text or ""
    truncated = len(body) > BODY_PREVIEW_LIMIT
    body_preview = body[:BODY_PREVIEW_LIMIT]
    unsupported_content = not bool(body.strip())

    return {
        "source": source,
        "message_id": message_id,
        "gmail_message_id": None,
        "thread_id": thread_id,
        "subject": subject,
        "sender": sender,
        "sender_email": sender_email,
        "received_at": received_at,
        "body_preview": body_preview,
        "gmail_labels": [],
        "truncated": truncated,
        "unsupported_content": unsupported_content,
        "has_attachments": has_attachments,
    }


def find_eml_files(path: Union[str, Path], recursive: bool = True) -> List[Path]:
    """Find .eml files under path, sorted deterministically."""
    target = Path(path).resolve()
    if target.is_file():
        return [target]
    if not target.is_dir():
        return []

    pattern = "**/*" if recursive else "*"
    files: List[Path] = []
    for candidate in target.glob(pattern):
        if candidate.is_file() and candidate.suffix.lower() == ".eml":
            files.append(candidate)
    return sorted(files, key=lambda p: str(p).lower())


def ingest_eml_bytes(
    db_obj: DB,
    raw_bytes: bytes,
    account_id: str = "local",
    source: str = "local",
) -> tuple[str, bool]:
    """Ingest raw .eml bytes into database.

    Returns a tuple of (local_message_id, is_new) where is_new is True if
    the message did not previously exist in the database.
    """
    decoded = parse_eml_bytes(raw_bytes, source=source)
    local_id = f"{account_id}:local:{decoded['message_id']}"
    is_new = db_obj.get_message(local_id) is None
    msg_id = db_obj.upsert_message(account_id, decoded)
    return msg_id, is_new


def ingest_path(
    db_obj: DB,
    path: Union[str, Path],
    account_id: Optional[str] = None,
    source: str = "local",
    recursive: bool = True,
    limit: Optional[int] = None,
) -> IngestResult:
    """Ingest .eml files from path into database.

    Defaults to account 'local'. Does not touch or overwrite existing decisions.
    """
    target = Path(path).resolve()
    if not target.exists():
        raise FileNotFoundError(f"Path does not exist: {path}")

    all_files = find_eml_files(target, recursive=recursive)
    files = all_files[:limit] if (limit is not None and limit > 0) else all_files

    acc_id = account_id or "local"
    acc_id = db_obj.get_or_create_account(
        email=acc_id,
        name="Local Mailbox" if acc_id == "local" else None,
        is_local=True,
    )

    result = IngestResult(total_found=len(all_files), account_id=acc_id)
    for file_path in files:
        try:
            raw_bytes = file_path.read_bytes()
            msg_id, is_new = ingest_eml_bytes(db_obj, raw_bytes, account_id=acc_id, source=source)
            result.message_ids.append(msg_id)
            result.ingested += 1
        except Exception as e:
            logger.warning("Failed to ingest %s: %s", file_path, e)
            result.failed += 1

    return result
