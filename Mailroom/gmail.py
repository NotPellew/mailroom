"""Gmail integration for Mailroom.

Reads messages, and (with the gmail.modify scope) creates labels and applies
them to messages. Destructive operations are never called: no trash, delete,
archive, spam, or send. Retention is represented as a label only.
Credentials are protected via keyring (DPAPI on Windows).
"""

import base64
import binascii
import json
import logging
import time
from datetime import date, datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import keyring
    import keyring.errors
except ImportError:
    keyring = None  # type: ignore[assignment]

from Mailroom.config import Config

logger = logging.getLogger(__name__)

# gmail.modify covers reading plus label changes on messages. It does not grant
# permanent delete or account/settings changes, and this adapter never calls
# trash/delete/archive/spam.
GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
GMAIL_SCOPES = [GMAIL_MODIFY_SCOPE]
# Historical read-only scope; kept for reference and older stored tokens.
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

KEYRING_SERVICE = "Mailroom"
# Non-secret pointer so later runs can look up the keyring entry (keyring cannot enumerate).
LAST_EMAIL_FILENAME = ".last_gmail_email"
DEFAULT_GMAIL_QUERY = Config.DEFAULT_SCAN_QUERY
DOCUMENTS_ATTACHMENT_QUERY = Config.DOCUMENTS_ATTACHMENT_QUERY


def resolve_scan_query(
    config_query: Optional[str] = None,
    cli_query: Optional[str] = None,
    documents_only: bool = False,
    default_query: Optional[str] = None,
) -> str:
    """Resolve the Gmail query string based on CLI flags and configuration.

    Precedence:
    1. Explicit cli_query overrides config_query.
    2. If neither cli_query nor config_query is provided, defaults to default_query or DEFAULT_GMAIL_QUERY.
    3. If documents_only is True, ensures the document attachment filter
       is included (appended if not already present).
    """
    fallback = default_query or DEFAULT_GMAIL_QUERY
    base = (cli_query.strip() if cli_query and cli_query.strip() else (config_query or fallback)).strip()
    if documents_only and DOCUMENTS_ATTACHMENT_QUERY not in base:
        base = f"{base} {DOCUMENTS_ATTACHMENT_QUERY}".strip()
    return base


BODY_PREVIEW_LIMIT = 4000
# Cap encoded body data before base64-decoding so a huge part cannot allocate
# an unbounded string just to be trimmed afterwards.
BODY_RAW_DATA_LIMIT = 64 * 1024
BODY_CACHE_EXPIRY_DAYS = 7
# Abort a bounded scan after this many consecutive messages.get failures so a
# persistent error cannot walk the whole mailbox.
GET_FAILURE_ABORT = 5
# Explicit scan --limit may go this high; default remains 100.
SCAN_MAX_MESSAGES = 1000
# messages.get(format=full) is quota-heavy; pause between calls.
GMAIL_GET_PAUSE_SECONDS = 0.35
GMAIL_RATE_LIMIT_RETRIES = 8
# Historical samples omit in:inbox so archived mail is included.
MONTHLY_QUERY_BASE = "-in:trash -in:spam -in:drafts"


def month_windows(years: int, today: Optional[date] = None) -> List[Tuple[date, date]]:
    """Return (start, end) month windows, oldest first, covering ``years`` * 12 months ending this month."""
    today = today or date.today()
    year, month = today.year, today.month
    windows: List[Tuple[date, date]] = []
    for _ in range(max(1, int(years)) * 12):
        start = date(year, month, 1)
        if month == 12:
            end = date(year + 1, 1, 1)
        else:
            end = date(year, month + 1, 1)
        windows.append((start, end))
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    windows.reverse()
    return windows


def gmail_month_query(start: date, end: date, base: Optional[str] = None) -> str:
    """Gmail search for [start, end) plus trash/spam/draft exclusions."""
    extra = (base or MONTHLY_QUERY_BASE).strip()
    return (
        f"{extra} after:{start.year}/{start.month:02d}/{start.day:02d} "
        f"before:{end.year}/{end.month:02d}/{end.day:02d}"
    )


class GmailError(Exception):
    """Raised for Gmail adapter errors (auth, fetch, decode)."""
    pass


def check_gmail_dependencies() -> Tuple[bool, List[str]]:
    """Check if optional Gmail dependencies are installed."""
    missing: List[str] = []
    try:
        import keyring  # noqa: F401
    except ImportError:
        missing.append("keyring")

    try:
        import googleapiclient.discovery  # noqa: F401
    except ImportError:
        missing.append("google-api-python-client")

    try:
        import google.oauth2.credentials  # noqa: F401
        import google.auth.exceptions  # noqa: F401
    except ImportError:
        missing.append("google-auth")

    try:
        import google_auth_oauthlib.flow  # noqa: F401
    except ImportError:
        missing.append("google-auth-oauthlib")

    return len(missing) == 0, missing


def ensure_gmail_dependencies() -> None:
    """Raise GmailError if optional Gmail dependencies are not installed."""
    ok, missing = check_gmail_dependencies()
    if not ok:
        missing_str = ", ".join(missing)
        raise GmailError(
            f"Gmail integration requires optional dependencies ({missing_str}). "
            "Please install them using: pip install 'mailroom[gmail]' or uv sync --extra gmail"
        )


def _get_keyring_username(email: str) -> str:
    return f"gmail:{email}"


def _last_email_path(config: Config):
    return config.config_dir / LAST_EMAIL_FILENAME


def _read_last_email(config: Config) -> Optional[str]:
    path = _last_email_path(config)
    if not path.exists():
        return None
    try:
        value = path.read_text(encoding="utf-8").strip()
        return value or None
    except OSError:
        return None


def _write_last_email(config: Config, email: str) -> None:
    try:
        _last_email_path(config).write_text(email, encoding="utf-8")
    except OSError as e:
        logger.warning("Failed to write last-email marker: %s", e)


def _store_credentials(email: str, credentials_json: str) -> None:
    """Store serialized credentials JSON in keyring (DPAPI-backed on Windows)."""
    ensure_gmail_dependencies()
    assert keyring is not None
    keyring.set_password(KEYRING_SERVICE, _get_keyring_username(email), credentials_json)


def _load_credentials(email: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Load stored credentials for a specific email. Returns the parsed token dict or None."""
    if email:
        ensure_gmail_dependencies()
        assert keyring is not None
        data = keyring.get_password(KEYRING_SERVICE, _get_keyring_username(email))
        if data:
            try:
                return json.loads(data)
            except json.JSONDecodeError:
                return None
        return None
    return None


def _delete_credentials(email: str) -> None:
    ensure_gmail_dependencies()
    assert keyring is not None
    try:
        keyring.delete_password(KEYRING_SERVICE, _get_keyring_username(email))
    except (getattr(getattr(keyring, "errors", None), "PasswordDeleteError", Exception)):
        pass


def _load_client_secrets(config_dir: Any) -> Optional[Dict[str, Any]]:
    """Look for credentials.json (desktop OAuth client) in the config directory."""
    try:
        p = config_dir / "credentials.json"
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.warning("Failed to load credentials.json: %s", e)
    return None


def _build_gmail_service(credentials_dict: Dict[str, Any]):
    """Build an authorized gmail service from a stored token dict."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        token=credentials_dict.get("token"),
        refresh_token=credentials_dict.get("refresh_token"),
        token_uri=credentials_dict.get("token_uri"),
        client_id=credentials_dict.get("client_id"),
        client_secret=credentials_dict.get("client_secret"),
        scopes=credentials_dict.get("scopes") or GMAIL_SCOPES,
    )
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    return service, creds


def _save_refreshed_credentials(email: str, creds) -> None:
    """Persist possibly refreshed token info."""
    try:
        token_json = creds.to_json()
        _store_credentials(email, token_json)
    except Exception as e:
        logger.warning("Failed to persist refreshed credentials: %s", e)


def _print_auth_notes(email: str) -> None:
    print(f"Authenticated as: {email}")
    if "@gmail.com" not in email.lower():
        print(
            "Note: This appears to be a Google Workspace / custom domain account. "
            "If you encounter access errors, ensure the OAuth client is internal/trusted "
            "or that your admin has granted the required scopes."
        )
    print("Refresh token is stored securely via the OS credential store (DPAPI on Windows).")
    print("Scope: gmail.modify (read + label changes; no delete/archive).")
    print("To reauthenticate later: python -m Mailroom auth --reauth")


def authenticate(config: Config, reauth: bool = False) -> str:
    """Perform desktop OAuth for Gmail label management.

    Returns the authenticated account email address.
    Stores credentials in keyring and writes the last-email marker used by later commands.
    """
    ensure_gmail_dependencies()
    if not reauth:
        try:
            _service, email = get_gmail_service(config)
            print(f"Already authenticated as: {email}")
            print("Use --reauth to force a new browser login.")
            return email
        except GmailError:
            pass

    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    client_config = _load_client_secrets(config.config_dir)
    if client_config is None:
        raise GmailError(
            "Desktop OAuth client secrets not found.\n"
            f"1. Create OAuth 2.0 Client ID (Desktop app) in Google Cloud Console for the Gmail API.\n"
            f"2. Download the JSON and save it as: {config.config_dir / 'credentials.json'}\n"
            f"3. Re-run: python -m Mailroom auth\n"
            "The gmail.modify scope will be requested (read + label changes only; "
            "no delete, archive, or send)."
        )

    try:
        flow = InstalledAppFlow.from_client_config(client_config, scopes=GMAIL_SCOPES)
        creds = flow.run_local_server(
            port=0,
            authorization_prompt_message="Please authorize Mailroom (Gmail label management).",
            success_message="Authorization complete. You may close this window.",
            open_browser=True,
        )
    except Exception as e:
        raise GmailError(
            f"OAuth flow failed: {e}\n"
            "If this is a Google Workspace account, the client may need to be added as a trusted internal app "
            "or authorized by an administrator. Personal @gmail.com accounts usually work without extra approval."
        ) from e

    try:
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        profile = service.users().getProfile(userId="me").execute()
        email = profile.get("emailAddress")
        if not email:
            raise GmailError("Failed to retrieve account email from Gmail profile.")
    except GmailError:
        raise
    except Exception as e:
        raise GmailError(f"Failed to fetch account profile after auth: {e}") from e

    try:
        _store_credentials(email, creds.to_json())
    except Exception as e:
        raise GmailError(f"Failed to securely store credentials via keyring: {e}") from e

    _write_last_email(config, email)
    _print_auth_notes(email)
    return email


def get_gmail_service(config: Config) -> Tuple[Any, str]:
    """Return an authorized Gmail service and the account email.

    Loads from keyring using the last-email marker. Refreshes if needed.
    """
    ensure_gmail_dependencies()
    from google.auth.exceptions import RefreshError

    stored_email = _read_last_email(config)
    token_dict = _load_credentials(stored_email) if stored_email else None
    email = stored_email if token_dict else None

    if not token_dict:
        raise GmailError(
            "No Gmail credentials found. Run: python -m Mailroom auth\n"
            "This will open a browser for one-time OAuth consent."
        )

    try:
        service, creds = _build_gmail_service(token_dict)

        profile = service.users().getProfile(userId="me").execute()
        email = profile.get("emailAddress")
        if not email:
            raise GmailError("Profile did not return an email address.")

        _save_refreshed_credentials(email, creds)
        _write_last_email(config, email)
        return service, email
    except RefreshError as e:
        if email:
            _delete_credentials(email)
        raise GmailError(
            f"Token refresh failed for {email or 'unknown'}. Re-run: python -m Mailroom auth --reauth\nDetails: {e}"
        ) from e
    except GmailError:
        raise
    except Exception as e:
        raise GmailError(f"Failed to build Gmail service: {e}") from e


def fetch_account_and_labels(service) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Fetch account identity and existing Gmail label metadata (read-only).

    Returns (profile_dict, list_of_label_dicts)
    """
    profile = service.users().getProfile(userId="me").execute()
    labels_resp = service.users().labels().list(userId="me").execute()
    labels = labels_resp.get("labels", [])
    label_meta = [
        {
            "id": label.get("id"),
            "name": label.get("name"),
            "type": label.get("type"),
        }
        for label in labels
    ]
    return profile, label_meta


def _extract_header(headers: Any, name: str) -> Optional[str]:
    name_lower = name.lower()
    for h in headers or []:
        if not isinstance(h, dict):
            continue
        if (h.get("name") or "").lower() == name_lower:
            return h.get("value")
    return None


def _decode_b64url(data: Any) -> Optional[bytes]:
    """Decode Gmail base64url body data safely.

    Returns the decoded bytes, or None when the input is not valid base64url.
    Gmail omits padding; already-padded input is also accepted.
    """
    if not isinstance(data, str) or not data:
        return None
    try:
        cleaned = "".join(data.split())
        pad = (-len(cleaned)) % 4
        if pad == 3:
            # A length of 4n+1 can never be valid base64.
            return None
        return base64.b64decode(cleaned + "=" * pad, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError, TypeError):
        return None


def _html_to_text(html: str) -> str:
    """Very simple, inert HTML-to-text conversion. No remote loads, no script execution."""
    if not html:
        return ""
    import re

    # Remove script/style entirely
    text = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.I | re.S)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    # Replace common block tags with newlines
    text = re.sub(r"</?(p|div|br|tr|li|h[1-6])[^>]*>", "\n", text, flags=re.I)
    # Strip remaining tags
    text = re.sub(r"<[^>]+>", " ", text)
    # Collapse whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*", "\n\n", text)
    return text.strip()


def _walk_parts(payload: Dict[str, Any]) -> Tuple[str, bool, bool]:
    """Walk Gmail payload to extract best text body.

    Returns (text, truncated, has_unsupported).
    Prefers text/plain over HTML. Skips attachments and non-text parts.
    """
    if not isinstance(payload, dict):
        return "", False, True
    if not payload:
        return "", False, True

    mime = (payload.get("mimeType") or "").lower()
    filename = payload.get("filename") or ""
    body = payload.get("body") or {}
    if not isinstance(body, dict):
        body = {}
    data = body.get("data") or ""
    size = body.get("size")
    parts = payload.get("parts") or []
    if not isinstance(parts, list):
        parts = []

    disp = ""
    for h in payload.get("headers") or []:
        if isinstance(h, dict) and (h.get("name") or "").lower() == "content-disposition":
            disp = (h.get("value") or "").lower()
            break
    is_attachment = bool(filename) or "attachment" in disp

    if is_attachment:
        return "", False, False

    if data and "text/" in mime:
        raw_capped = False
        if isinstance(data, str) and len(data) > BODY_RAW_DATA_LIMIT:
            # Keep a whole number of base64 characters so decoding stays valid.
            data = data[: BODY_RAW_DATA_LIMIT - (BODY_RAW_DATA_LIMIT % 4)]
            raw_capped = True
        raw = _decode_b64url(data)
        if raw is None:
            # Malformed base64 must not be turned into garbled text.
            return "", False, True
        size_truncated = isinstance(size, int) and size > len(raw)
        decoded = raw.decode("utf-8", errors="replace")
        if "html" in mime:
            decoded = _html_to_text(decoded)
        truncated = raw_capped or size_truncated or len(decoded) > BODY_PREVIEW_LIMIT
        return decoded[:BODY_PREVIEW_LIMIT], truncated, False

    if not parts and "text/" in mime and isinstance(size, int) and size > 0 and not data:
        return "", True, True

    plain_text = ""
    html_text = ""
    any_trunc = False
    unsupported = False

    for p in parts:
        t, tr, uns = _walk_parts(p)
        any_trunc = any_trunc or tr
        unsupported = unsupported or uns
        if not t:
            continue
        pmime = (p.get("mimeType") or "").lower()
        if "plain" in pmime:
            if not plain_text:
                plain_text = t
        elif "html" in pmime:
            if not html_text:
                html_text = t
        elif not plain_text:
            plain_text = t

    if plain_text:
        return plain_text, any_trunc, unsupported
    if html_text:
        return html_text, any_trunc, unsupported

    return "", any_trunc, True


def decode_gmail_message(msg_resource: Dict[str, Any]) -> Dict[str, Any]:
    """Safely decode a Gmail message resource (format=full) into normalized fields.

    Never executes attachments or loads remote content. Produces inert text only.
    """
    if not isinstance(msg_resource, dict):
        return {
            "gmail_message_id": None,
            "thread_id": None,
            "subject": "",
            "sender": "",
            "sender_email": "",
            "received_at": None,
            "body_preview": "",
            "gmail_labels": [],
            "truncated": False,
            "unsupported_content": True,
            "has_attachments": False,
        }

    gmail_id = msg_resource.get("id")
    thread_id = msg_resource.get("threadId")
    label_ids = msg_resource.get("labelIds") or []

    payload = msg_resource.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}
    headers = payload.get("headers") or []
    if not isinstance(headers, list):
        headers = []

    subject = _extract_header(headers, "Subject") or ""
    from_header = _extract_header(headers, "From") or ""
    date_header = _extract_header(headers, "Date") or ""

    display_name, addr = parseaddr(from_header)
    sender = display_name or addr or from_header
    sender_email = addr or ""

    received_at = None
    try:
        if date_header:
            dt = parsedate_to_datetime(date_header)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            received_at = dt.astimezone(timezone.utc).isoformat()
    except Exception:
        # Keep None; we still store the message
        received_at = None

    body_text, truncated, unsupported = _walk_parts(payload)

    # Detect attachments at top level or parts
    def _has_attachments(p: Any) -> bool:
        if not isinstance(p, dict) or not p:
            return False
        fn = p.get("filename")
        if fn:
            return True
        for h in p.get("headers") or []:
            if (
                isinstance(h, dict)
                and (h.get("name") or "").lower() == "content-disposition"
                and "attachment" in (h.get("value") or "").lower()
            ):
                return True
        parts = p.get("parts") or []
        if isinstance(parts, list):
            for sub in parts:
                if _has_attachments(sub):
                    return True
        return False

    has_attach = _has_attachments(payload)

    # Final truncation: if body longer than cap even after walk
    if len(body_text) > BODY_PREVIEW_LIMIT:
        body_text = body_text[:BODY_PREVIEW_LIMIT]
        truncated = True

    return {
        "gmail_message_id": gmail_id,
        "thread_id": thread_id,
        "subject": subject,
        "sender": sender,
        "sender_email": sender_email,
        "received_at": received_at,
        "body_preview": body_text,
        "gmail_labels": label_ids,
        "truncated": bool(truncated),
        "unsupported_content": bool(unsupported and not body_text),
        "has_attachments": bool(has_attach),
    }


def _http_status(exc: Exception) -> Optional[int]:
    resp = getattr(exc, "resp", None)
    if resp is not None:
        status = getattr(resp, "status", None)
        if status is not None:
            return int(status)
    status = getattr(exc, "status_code", None)
    return int(status) if status is not None else None


def _is_rate_limit_error(exc: Exception) -> bool:
    status = _http_status(exc)
    compact = str(exc).lower().replace(" ", "")
    hinted = (
        "quotaexceeded" in compact
        or "ratelimitexceeded" in compact
        or "userratelimitexceeded" in compact
    )
    if not hinted:
        return False
    return status in (None, 403, 429)


def _execute_with_retry(request, what: str):
    """Run a Gmail API request, backing off on per-user quota."""
    last_error: Optional[Exception] = None
    for attempt in range(GMAIL_RATE_LIMIT_RETRIES):
        try:
            return request.execute()
        except Exception as e:
            last_error = e
            if not _is_rate_limit_error(e) or attempt >= GMAIL_RATE_LIMIT_RETRIES - 1:
                raise
            wait = min(15 * (2 ** attempt), 120)
            logger.warning("Gmail quota while %s; waiting %ss (attempt %s)", what, wait, attempt + 1)
            print(f"Gmail quota hit ({what}); waiting {wait}s then retrying...", flush=True)
            time.sleep(wait)
    raise GmailError(f"Gmail {what} failed: {last_error}") from last_error


def fetch_bounded_sample(
    service,
    query: Optional[str] = None,
    limit: Optional[int] = None,
    skip_ids: Optional[set] = None,
    on_message: Optional[Callable[[Dict[str, Any]], None]] = None,
    on_skip: Optional[Callable[[str], None]] = None,
) -> Tuple[List[Dict[str, Any]], str]:
    """Fetch up to `limit` messages using the given query (or default).

    Returns (list_of_decoded_messages, sample_time_iso).
    When ``on_message`` is set, the returned list is empty so the sample is
    not held twice in RAM.
    Never fetches the full mailbox. Paginates only until cap.
    ``skip_ids`` are Gmail message ids already cached locally; they count
    toward the cap but are not re-downloaded (resume after a quota stop).
    ``on_message`` is called with each decoded message as soon as it arrives
    so callers can store or classify without waiting for the full sample.
    ``on_skip`` is called with a Gmail id that was already cached (not re-fetched)
    so suggest-labels/classify can still use the local body.
    """
    if query is None:
        query = DEFAULT_GMAIL_QUERY
    if limit is None:
        limit = 100
    limit = max(1, min(int(limit), SCAN_MAX_MESSAGES))
    skip_ids = set(skip_ids or ())
    keep_in_memory = on_message is None

    messages = []
    page_token = None
    fetched = 0
    skipped = 0
    consecutive_failures = 0
    sample_time = datetime.now(timezone.utc).isoformat()

    while fetched < limit:
        page_size = min(20, limit - fetched)  # small pages for safety
        try:
            resp = _execute_with_retry(
                service.users()
                .messages()
                .list(userId="me", q=query, maxResults=page_size, pageToken=page_token),
                "listing messages",
            )
        except Exception as e:
            raise GmailError(f"Gmail list failed: {e}") from e

        ids = [m["id"] for m in resp.get("messages", [])]
        if not ids:
            break

        for mid in ids:
            if fetched >= limit:
                break
            if mid in skip_ids:
                fetched += 1
                skipped += 1
                if on_skip is not None:
                    on_skip(mid)
                continue
            try:
                full = _execute_with_retry(
                    service.users().messages().get(userId="me", id=mid, format="full"),
                    f"getting {mid}",
                )
            except Exception as e:
                # Count failures toward the cap (otherwise a persistent error
                # could keep paginating forever) and stop after too many in a row.
                fetched += 1
                consecutive_failures += 1
                logger.warning("Skipping message %s: get failed: %s", mid, e)
                if consecutive_failures >= GET_FAILURE_ABORT:
                    raise GmailError(
                        f"Gmail get failed {consecutive_failures} times in a row; stopping scan"
                    ) from e
                continue

            decoded = decode_gmail_message(full)
            if "labelIds" in full:
                decoded["gmail_labels"] = full.get("labelIds") or decoded["gmail_labels"]
            decoded["fetched_at"] = sample_time
            del full
            if on_message is not None:
                on_message(decoded)
            if keep_in_memory:
                messages.append(decoded)
            fetched += 1
            consecutive_failures = 0
            time.sleep(GMAIL_GET_PAUSE_SECONDS)

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    if skipped:
        logger.info("Scan reused %s already-cached message(s)", skipped)
    return messages, sample_time


def get_stored_scopes(config: Config) -> List[str]:
    """Return the OAuth scopes recorded with the stored credential (may be empty)."""
    stored_email = _read_last_email(config)
    token = _load_credentials(stored_email) if stored_email else None
    if not token:
        return []
    return [str(s) for s in (token.get("scopes") or [])]


def list_user_labels(service) -> Dict[str, str]:
    """Return {label name: label id} for the account's labels."""
    resp = _execute_with_retry(service.users().labels().list(userId="me"), "listing labels")
    out: Dict[str, str] = {}
    for item in resp.get("labels", []) or []:
        name = item.get("name")
        label_id = item.get("id")
        if name and label_id:
            out[name] = label_id
    return out


def ensure_label(service, name: str, existing: Optional[Dict[str, str]] = None) -> str:
    """Create a user label by name if missing and return its id. Idempotent.

    Reuses an existing label with the same name. Only ``labels.create`` is called.
    """
    label_map = existing if existing is not None else list_user_labels(service)
    if name in label_map:
        return label_map[name]
    body = {
        "name": name,
        "labelListVisibility": "labelShow",
        "messageListVisibility": "show",
    }
    created = _execute_with_retry(
        service.users().labels().create(userId="me", body=body), f"creating label {name}"
    )
    label_id = created.get("id")
    if not label_id:
        raise GmailError(f"Gmail did not return an id for label {name!r}")
    label_map[name] = label_id
    return label_id


def modify_message_labels(
    service,
    gmail_message_id: str,
    add_label_ids: Optional[List[str]] = None,
    remove_label_ids: Optional[List[str]] = None,
):
    """Add/remove labels on one message. Never deletes, archives, or marks spam.

    Only ``messages.modify`` is called; passing no add/remove ids is a no-op.
    """
    body: Dict[str, Any] = {}
    if add_label_ids:
        body["addLabelIds"] = list(add_label_ids)
    if remove_label_ids:
        body["removeLabelIds"] = list(remove_label_ids)
    if not body:
        return None
    return _execute_with_retry(
        service.users().messages().modify(userId="me", id=gmail_message_id, body=body),
        f"updating labels for {gmail_message_id}",
    )


def fetch_message_label_ids(service, gmail_message_id: str) -> List[str]:
    """Return a message's current label ids (metadata only; no body fetch)."""
    msg = _execute_with_retry(
        service.users().messages().get(userId="me", id=gmail_message_id, format="minimal"),
        f"reading labels for {gmail_message_id}",
    )
    return list(msg.get("labelIds") or [])


def clear_stored_auth(email: Optional[str] = None) -> None:
    """Remove stored credentials. If email is None, best-effort clear of last known."""
    # Without enumeration we can only clear if email supplied.
    # The .last_gmail_email file can be used to hint.
    if email:
        _delete_credentials(email)
        return
    # Nothing else we can safely do without the email.
