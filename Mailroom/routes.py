"""Flask routes for Mailroom review page."""

import io
import logging
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional
from urllib.parse import quote

from flask import Blueprint, render_template, jsonify, current_app, request, Response

from Mailroom import security
from Mailroom.config import ConfigError, is_loopback_url

logger = logging.getLogger(__name__)

bp = Blueprint("main", __name__)

_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _get_config():
    """Return the loaded config, creating a default one if absent."""
    config = current_app.config.get("MAILROOM_CONFIG")
    if config is None:
        from Mailroom.config import Config

        config = Config()
    return config


@bp.before_app_request
def _enforce_local_request_safety():
    """Reject non-loopback hosts and cross-site state-changing requests."""
    if not security.is_allowed_host_header(request.host):
        return jsonify({"error": "Unexpected Host header"}), 400

    if request.method in _MUTATING_METHODS:
        origin = request.headers.get("Origin")
        referer = request.headers.get("Referer")
        if origin and not security.is_allowed_origin_or_referer(origin):
            return jsonify({"error": "Cross-origin request rejected"}), 403
        if not origin and referer and not security.is_allowed_origin_or_referer(referer):
            return jsonify({"error": "Cross-origin request rejected"}), 403
        # Require the per-instance token for every mutation, browser or not.
        expected = current_app.config.get("CSRF_TOKEN")
        provided = request.headers.get("X-CSRF-Token")
        if not security.csrf_token_matches(expected, provided):
            return jsonify({"error": "Missing or invalid CSRF token"}), 403
    return None


def get_status(db_path: str) -> Dict[str, Any]:
    """Get current application status safely.

    Args:
        db_path: Path to the database file

    Returns:
        Dictionary with status counts
    """
    default_status = {
        "accounts": 0,
        "gmail_accounts": 0,
        "local_accounts": 0,
        "mode": "local",
        "messages": 0,
        "proposals_pending": 0,
        "decisions_pending": 0,
        "reviewed": 0,
        "exportable": 0,
        "skipped": 0,
        "unclassified": 0,
        "db_initialized": False,
    }

    db_file = Path(db_path)
    if not db_file.exists():
        return default_status

    try:
        conn = sqlite3.connect(str(db_file), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            # Check if required tables exist
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('accounts', 'messages', 'proposals', 'decisions')"
            )
            tables = {row["name"] for row in cursor.fetchall()}
            if not {"accounts", "messages", "proposals", "decisions"}.issubset(tables):
                return default_status

            cursor.execute(
                """
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN gmail_id IS NOT NULL THEN 1 ELSE 0 END) as gmail_count
                FROM accounts
                """
            )
            account_row = cursor.fetchone()
            account_count = account_row["total"]
            gmail_account_count = account_row["gmail_count"] or 0
            local_account_count = account_count - gmail_account_count

            cursor.execute("SELECT COUNT(*) as count FROM messages")
            message_count = cursor.fetchone()["count"]

            # Messages that have a proposal and no decision yet.
            # COUNT DISTINCT so multiple proposal versions count as one pending item.
            cursor.execute(
                """
                SELECT COUNT(DISTINCT p.message_id) as count
                FROM proposals p
                WHERE NOT EXISTS (
                    SELECT 1 FROM decisions d WHERE d.message_id = p.message_id
                )
                """
            )
            pending_count = cursor.fetchone()["count"]

            # decisions pending check
            cursor.execute("SELECT COUNT(*) as count FROM decisions WHERE status = 'pending'")
            pending_decisions = cursor.fetchone()["count"]

            # decisions that carry a final human choice
            cursor.execute(
                """
                SELECT
                    COUNT(*) as total_reviewed,
                    COALESCE(SUM(CASE WHEN status IN ('accepted', 'corrected') THEN 1 ELSE 0 END), 0) as exportable_count,
                    COALESCE(SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END), 0) as skipped_count
                FROM decisions
                WHERE status IN ('accepted', 'corrected', 'skipped')
                """
            )
            decision_row = cursor.fetchone()
            reviewed_count = (decision_row["total_reviewed"] if decision_row else 0) or 0
            exportable_count = (decision_row["exportable_count"] if decision_row else 0) or 0
            skipped_count = (decision_row["skipped_count"] if decision_row else 0) or 0

            # Messages that have body text and no proposal yet (unclassified)
            cursor.execute(
                """
                SELECT COUNT(*) as count
                FROM messages m
                WHERE m.body_preview IS NOT NULL AND m.body_preview != ''
                  AND NOT EXISTS (
                      SELECT 1 FROM proposals p WHERE p.message_id = m.id
                  )
                """
            )
            unclassified_count = cursor.fetchone()["count"]

            return {
                "accounts": account_count,
                "gmail_accounts": gmail_account_count,
                "local_accounts": local_account_count,
                "mode": "gmail" if gmail_account_count else "local",
                "messages": message_count,
                "proposals_pending": pending_count,
                "decisions_pending": pending_decisions,
                "reviewed": reviewed_count,
                "exportable": exportable_count,
                "skipped": skipped_count,
                "unclassified": unclassified_count,
                "db_initialized": True,
            }
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return default_status


@bp.route("/")
def index():
    """Render the review page."""
    return render_template("index.html", csrf_token=current_app.config.get("CSRF_TOKEN", ""))


@bp.route("/api/csrf")
def api_csrf():
    """Expose the per-instance CSRF token to the same-origin review page."""
    return jsonify({"csrf_token": current_app.config.get("CSRF_TOKEN", "")})


@bp.route("/health")
def health():
    """Health check endpoint."""
    return jsonify({"status": "healthy", "version": "0.1.0"})


@bp.route("/api/labels")
def api_labels():
    """API endpoint to get configured label definitions."""
    return jsonify({"labels": _get_config().labels})


def _label_shape_error(data: Any) -> Optional[str]:
    """Shape-only label checks; required-field and uniqueness rules live in Config.validate."""
    if not isinstance(data, dict):
        return "Label must be a JSON object"
    for field in ("id", "name", "description", "axis"):
        value = data.get(field)
        if value is not None and not isinstance(value, str):
            return f"{field} must be a string"
    for field in ("examples", "exclusions"):
        value = data.get(field)
        if value is not None and (
            not isinstance(value, list) or not all(isinstance(item, str) for item in value)
        ):
            return f"{field} must be a list of strings"
    return None


def _current_labels(config) -> List[Dict[str, Any]]:
    """Return a mutable copy of the live label definitions."""
    return [dict(label) for label in config.labels]


@bp.route("/api/labels", methods=["POST"])
def api_create_label():
    """Create a label definition; validated and persisted live, no restart needed."""
    config = _get_config()
    data = request.get_json(silent=True)
    shape_error = _label_shape_error(data)
    if shape_error:
        return jsonify({"error": shape_error}), 400

    labels = _current_labels(config)
    labels.append(dict(data))
    try:
        config.set_labels(labels)
    except ConfigError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"labels": config.labels})


@bp.route("/api/labels/<path:label_id>", methods=["PUT"])
def api_update_label(label_id):
    """Update one label definition; the id itself may not change."""
    config = _get_config()
    data = request.get_json(silent=True)
    shape_error = _label_shape_error(data)
    if shape_error:
        return jsonify({"error": shape_error}), 400

    labels = _current_labels(config)
    index = next((i for i, label in enumerate(labels) if label.get("id") == label_id), None)
    if index is None:
        return jsonify({"error": f"Unknown label id: {label_id}"}), 404

    new_id = data.get("id")
    if new_id is not None and new_id != label_id:
        return jsonify({"error": "Label id cannot be changed"}), 400

    updated = dict(labels[index])
    updated.update(data)
    labels[index] = updated
    try:
        config.set_labels(labels)
    except ConfigError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"labels": config.labels})


@bp.route("/api/labels/<path:label_id>", methods=["DELETE"])
def api_delete_label(label_id):
    """Remove one label definition; refused while saved decisions reference it."""
    config = _get_config()
    labels = _current_labels(config)
    remaining = [label for label in labels if label.get("id") != label_id]
    if len(remaining) == len(labels):
        return jsonify({"error": f"Unknown label id: {label_id}"}), 404

    from Mailroom.db import DB

    db_obj = DB(config.database_path)
    try:
        in_use = db_obj.count_decisions_using_label(label_id)
    finally:
        db_obj.close()
    if in_use:
        return jsonify(
            {
                "error": (
                    f"Cannot remove label '{label_id}': it is referenced by "
                    f"{in_use} saved decision(s)."
                ),
                "in_use": in_use,
            }
        ), 400

    try:
        config.set_labels(remaining)
    except ConfigError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"labels": config.labels})


@bp.route("/api/ingest", methods=["POST"])
def api_ingest():
    """Ingest .eml files or .zip archives uploaded via drag-and-drop or file picker."""
    config = _get_config()
    from Mailroom.db import DB
    from Mailroom.ingest import ingest_eml_bytes

    files = request.files.getlist("files")
    if not files and "file" in request.files:
        files = [request.files["file"]]

    if not files or all(not f.filename for f in files):
        return jsonify({"error": "No files provided"}), 400

    db_obj = DB(config.database_path)
    try:
        acc_id = db_obj.get_or_create_account(
            email="local",
            name="Local Mailbox",
            is_local=True,
        )

        total_received = 0
        ingested = 0
        duplicates_or_existing = 0
        failed = 0
        message_ids: List[str] = []

        MAX_EML_SIZE = 10 * 1024 * 1024  # 10 MB
        MAX_ZIP_UNCOMPRESSED = 50 * 1024 * 1024  # 50 MB
        MAX_ZIP_FILES = 500

        for f in files:
            filename = f.filename or ""
            if not filename:
                continue
            lower_name = filename.lower()

            if lower_name.endswith(".zip"):
                try:
                    zip_bytes = f.read()
                    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                        uncompressed_total = 0
                        file_count = 0
                        eml_entries = []
                        for info in zf.infolist():
                            if info.is_dir():
                                continue
                            fname = info.filename.lower()
                            if (
                                fname.startswith("__macosx")
                                or "/__macosx" in fname
                                or ".ds_store" in fname
                            ):
                                continue
                            if fname.endswith(".zip"):
                                return jsonify({"error": "Nested zip archives are not allowed"}), 400
                            if fname.endswith(".eml"):
                                file_count += 1
                                if file_count > MAX_ZIP_FILES:
                                    return (
                                        jsonify(
                                            {
                                                "error": (
                                                    f"Zip archive exceeds maximum limit of {MAX_ZIP_FILES} files"
                                                )
                                            }
                                        ),
                                        400,
                                    )
                                uncompressed_total += info.file_size
                                if uncompressed_total > MAX_ZIP_UNCOMPRESSED:
                                    return (
                                        jsonify(
                                            {
                                                "error": (
                                                    "Zip archive exceeds uncompressed size limit of 50 MB"
                                                )
                                            }
                                        ),
                                        400,
                                    )
                                if info.file_size > MAX_EML_SIZE:
                                    return (
                                        jsonify(
                                            {
                                                "error": (
                                                    f"File in zip exceeds maximum limit of 10 MB: {info.filename}"
                                                )
                                            }
                                        ),
                                        400,
                                    )
                                eml_entries.append(info)

                        for info in eml_entries:
                            total_received += 1
                            try:
                                raw_bytes = zf.read(info)
                                msg_id, is_new = ingest_eml_bytes(db_obj, raw_bytes, account_id=acc_id)
                                message_ids.append(msg_id)
                                if is_new:
                                    ingested += 1
                                else:
                                    duplicates_or_existing += 1
                            except Exception as e:
                                logger.warning("Failed to ingest zip entry %s: %s", info.filename, e)
                                failed += 1
                except zipfile.BadZipFile:
                    return jsonify({"error": "Corrupted or invalid zip archive"}), 400
            elif lower_name.endswith(".eml") or f.content_type in (
                "message/rfc822",
                "application/octet-stream",
                "text/plain",
            ):
                total_received += 1
                try:
                    raw_bytes = f.read()
                    if len(raw_bytes) > MAX_EML_SIZE:
                        return (
                            jsonify(
                                {"error": f"File exceeds maximum limit of 10 MB: {filename}"}
                            ),
                            400,
                        )
                    msg_id, is_new = ingest_eml_bytes(db_obj, raw_bytes, account_id=acc_id)
                    message_ids.append(msg_id)
                    if is_new:
                        ingested += 1
                    else:
                        duplicates_or_existing += 1
                except Exception as e:
                    logger.warning("Failed to ingest file %s: %s", filename, e)
                    failed += 1
            else:
                total_received += 1
                failed += 1

        return jsonify({
            "status": "success",
            "total_received": total_received,
            "ingested": ingested,
            "duplicates_or_existing": duplicates_or_existing,
            "failed": failed,
            "account_id": acc_id,
            "message_ids": message_ids,
        })
    finally:
        db_obj.close()


@bp.route("/api/status")
def api_status():
    """API endpoint to get current status."""
    config = current_app.config.get("MAILROOM_CONFIG")
    if config:
        db_path = config.database_path
    else:
        from Mailroom.config import Config
        db_path = Config().database_path
    return jsonify(get_status(db_path))


@bp.route("/api/probe-model")
def api_probe_model():
    """API endpoint to probe the model endpoint and get model info."""
    config = current_app.config.get("MAILROOM_CONFIG")
    if config:
        model_endpoint = config.model_endpoint
        timeout = config.timeout
        provider = config.model_provider
    else:
        from Mailroom.config import Config
        cfg = Config()
        model_endpoint = cfg.model_endpoint
        timeout = cfg.timeout
        provider = cfg.model_provider

    try:
        from Mailroom.classification import probe_model_endpoint

        result = probe_model_endpoint(model_endpoint, timeout=timeout, provider=provider)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _classify_direct_payload(config, data):
    """Classify a direct subject/body payload without touching SQLite or Gmail."""
    from Mailroom.classification import ClassificationError, proposal_payload
    from Mailroom.classifier import EmailClassifier

    for field in ("subject", "body", "sender", "sender_email"):
        value = data.get(field)
        if value is not None and not isinstance(value, str):
            return jsonify({"error": f"{field} must be a string"}), 400

    body = data.get("body") or ""
    if not body.strip():
        return jsonify({"error": "body must not be empty"}), 400

    try:
        classifier = EmailClassifier(
            config=config,
            model_endpoint=data.get("model_endpoint"),
            timeout=config.timeout,
            model_id=config.model_id,
            provider=data.get("provider"),
        )
        proposal = classifier.classify(
            labels=config.labels,
            subject=data.get("subject") or "",
            sender=data.get("sender") or "",
            sender_email=data.get("sender_email") or "",
            body=body,
        )
    except ClassificationError as e:
        return jsonify({"error": str(e)}), 500

    return jsonify(proposal_payload(proposal, config.labels))


@bp.route("/api/classify/pending", methods=["GET"])
def api_classify_pending():
    """API endpoint to list cached messages pending classification."""
    config = _get_config()
    limit_arg = request.args.get("limit", "1000")
    try:
        limit = int(limit_arg)
    except (ValueError, TypeError):
        limit = 1000
    limit = max(1, min(limit, 1000))

    from Mailroom.db import DB

    db_obj = DB(config.database_path)
    try:
        targets = db_obj.list_messages_pending_classification(limit)
        cursor = db_obj.conn.cursor()
        cursor.execute(
            """
            SELECT COUNT(*) as count
            FROM messages m
            WHERE m.body_preview IS NOT NULL AND m.body_preview != ''
              AND NOT EXISTS (
                  SELECT 1 FROM proposals p WHERE p.message_id = m.id
              )
            """
        )
        total_pending = cursor.fetchone()["count"]

        pending_items = [
            {
                "id": t["id"],
                "account_id": t["account_id"],
                "subject": t.get("subject") or "",
            }
            for t in targets
        ]

        return jsonify({
            "total": total_pending,
            "count": len(pending_items),
            "pending": pending_items,
        })
    finally:
        db_obj.close()


@bp.route("/api/classify", methods=["POST"])
def api_classify():
    """API endpoint to classify a message.

    Accepts either a cached message (``message_id``) or a direct payload
    (``subject``/``body``/``sender``) that needs no stored row. Direct
    classification is stateless and persists nothing.
    """
    config = _get_config()

    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Invalid request: missing JSON body"}), 400

        message_id = data.get("message_id")
        if not message_id and data.get("body") is None:
            return jsonify(
                {"error": "Provide either message_id or a direct body payload"}
            ), 400

        # Optional model endpoint override (loopback only, like config).
        endpoint_override = data.get("model_endpoint")
        if endpoint_override and not is_loopback_url(endpoint_override):
            return jsonify({"error": "model_endpoint must be a loopback http(s) URL"}), 400

        if not message_id:
            return _classify_direct_payload(config, data)

        # Parse message ID
        if ":" not in message_id:
            return jsonify({"error": f"Invalid message ID format: {message_id}. Expected: account_id:message_id"}), 400
        account_id, msg_part = message_id.split(":", 1)

        labels = config.labels
        model_endpoint = endpoint_override or config.model_endpoint
        provider = data.get("provider") if endpoint_override else config.model_provider
        label_ids = [label["id"] for label in labels]

        # Load message from database
        from Mailroom.db import DB

        db_obj = DB(config.database_path)
        try:
            message = db_obj.get_message(message_id) or db_obj.get_message_by_gmail_id(account_id, msg_part)
            if not message:
                return jsonify({"error": f"Message not found: {message_id}"}), 404

            body = message["body_preview"] or ""
            if not body:
                return jsonify({"error": f"Message has no body text to classify: {message_id}"}), 400

            # Perform classification on an inert From/Subject/body block.
            from Mailroom.classification import (
                build_mail_block,
                classify_message,
                label_definition_version,
                proposal_payload,
                prompt_version,
            )

            email_text = build_mail_block(
                sender=message["sender"] or "",
                sender_email=message["sender_email"] or "",
                subject=message["subject"] or "",
                body=body,
            )
            prompt_ver = prompt_version(labels)
            label_ver = label_definition_version(labels)

            proposal = classify_message(
                email_text=email_text,
                label_ids=label_ids,
                model_endpoint=model_endpoint,
                timeout=config.timeout,
                model_id=config.model_id,
                labels=labels,
                provider=provider,
            )

            # Persist the proposal separately from final decisions.
            label_names = [label["name"] for label in labels if label["id"] in proposal.label_ids]
            proposal_id = db_obj.insert_proposal(
                account_id=message["account_id"],
                message_id=message["id"],
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

            # Return classification result
            result = {**proposal_payload(proposal, labels), "proposal_id": proposal_id}
            return jsonify(result)

        finally:
            db_obj.close()

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/proposals")
def api_proposals():
    """API endpoint to get proposals for a message.

    Query parameters:
    - message_id: Message ID to get proposals for
    """
    config = current_app.config.get("MAILROOM_CONFIG")
    if config:
        db_path = config.database_path
    else:
        from Mailroom.config import Config
        db_path = Config().database_path

    message_id = request.args.get("message_id")
    if not message_id:
        return jsonify({"error": "Missing required parameter: message_id"}), 400

    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")

        cursor = conn.cursor()

        if ":" in message_id:
            # Full message ID format: account_id:gmail_message_id
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
        else:
            # Account ID only: return the most recent proposals for that account.
            cursor.execute(
                """
                SELECT id, message_id, account_id, label_ids, label_names, reason,
                       confidence, abstain, source, model_version, prompt_version,
                       label_definition_version, created_at
                FROM proposals
                WHERE account_id = ?
                ORDER BY rowid DESC
                LIMIT 10
                """,
                (message_id,),
            )

        proposals = [dict(row) for row in cursor.fetchall()]

        return jsonify({"proposals": proposals})

    finally:
        conn.close()


@bp.route("/api/proposals", methods=["DELETE"])
def api_delete_proposals():
    """API endpoint to delete all proposals for a message.

    Request body:
    {
        "message_id": "account_id:gmail_message_id"
    }
    """
    config = current_app.config.get("MAILROOM_CONFIG")
    if config:
        db_path = config.database_path
    else:
        from Mailroom.config import Config
        db_path = Config().database_path

    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid request: missing JSON body"}), 400

    message_id = data.get("message_id")
    if not message_id:
        return jsonify({"error": "Missing required field: message_id"}), 400

    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")

        cursor = conn.cursor()
        cursor.execute(
            """
            DELETE FROM proposals
            WHERE message_id = ?
            """,
            (message_id,),
        )
        deleted = cursor.rowcount

        conn.commit()
        return jsonify({"deleted": deleted})

    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Step 4: review list, decisions, and export
# ---------------------------------------------------------------------------

def _gmail_message_url(
    account_email: Optional[str],
    gmail_message_id: Optional[str],
    source: str = "gmail",
) -> Optional[str]:
    """Build a Gmail web link for a message under the connected account."""
    if source != "gmail" or not gmail_message_id:
        return None
    base = "https://mail.google.com/mail/u/0/"
    if account_email:
        base += "?authuser=" + quote(account_email, safe="")
    return base + "#all/" + quote(gmail_message_id or "", safe="")


def _review_item_payload(item: Dict[str, Any], include_body: bool = False) -> Dict[str, Any]:
    """Shape a database review item for the JSON API."""
    proposal = None
    if item.get("has_proposal"):
        proposal = {
            "label_ids": item.get("proposal_label_ids") or [],
            "label_names": item.get("proposal_label_names") or [],
            "reason": item.get("proposal_reason"),
            "abstain": item.get("abstain", False),
            "source": item.get("proposal_source"),
            "created_at": item.get("proposal_created_at"),
        }

    decision = None
    if item.get("decision_status"):
        decision = {
            "status": item.get("decision_status"),
            "label_ids": item.get("decision_label_ids") or [],
            "user_notes": item.get("decision_notes"),
            "updated_at": item.get("decision_updated_at"),
        }

    # A newer suggestion is one whose id differs from the one the saved
    # choice was based on (the older of the two is never lost).
    newer_proposal = bool(
        proposal
        and decision
        and item.get("proposal_id") != item.get("decision_proposal_id")
    )

    source = item.get("source") or ("gmail" if item.get("gmail_message_id") else "local")

    payload = {
        "message_id": item.get("message_id"),
        "source": source,
        "gmail_message_id": item.get("gmail_message_id"),
        "thread_id": item.get("thread_id"),
        "account_id": item.get("account_id"),
        "account_email": item.get("account_email"),
        "subject": item.get("subject"),
        "sender": item.get("sender"),
        "sender_email": item.get("sender_email"),
        "received_at": item.get("received_at"),
        "has_body": item.get("has_body", False),
        "truncated": item.get("truncated", False),
        "unsupported_content": item.get("unsupported_content", False),
        "has_attachments": item.get("has_attachments", False),
        "proposal": proposal,
        "suggestion_state": item.get("suggestion_state"),
        "abstain": item.get("abstain", False),
        "review_status": item.get("review_status"),
        "decision": decision,
        "newer_proposal_than_decision": newer_proposal,
        "gmail_url": _gmail_message_url(
            item.get("account_email"), item.get("gmail_message_id"), source=source
        ),
    }
    if include_body:
        payload["body_preview"] = item.get("body_preview")
    return payload


def _label_id_set(config) -> set:
    return {label["id"] for label in config.labels}


def _label_name_map(config) -> Dict[str, str]:
    return {label["id"]: label["name"] for label in config.labels}


@bp.route("/api/review")
def api_review_list():
    """List messages with their latest proposal and saved decision.

    Query parameters: ``status`` (unreviewed/pending/accepted/corrected/skipped),
    ``label`` (proposed label id), ``state`` (proposed/uncertain/error),
    ``disagrees`` (1/true to show reviewed mail whose latest suggestion differs
    from the saved choice on the kind axis), ``limit`` (default 50, max 200)
    and ``offset`` for pagination.
    """
    config = _get_config()
    status = request.args.get("status")
    label = request.args.get("label")
    state = request.args.get("state")
    disagrees = request.args.get("disagrees") in ("1", "true", "yes", "on")

    try:
        limit = int(request.args.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 200))
    try:
        offset = int(request.args.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0
    offset = max(0, offset)

    from Mailroom.db import DB

    db_obj = DB(config.database_path)
    try:
        items = db_obj.list_review_items(
            status=status, label=label, state=state, disagreement=disagrees,
            limit=limit, offset=offset,
        )
        total = db_obj.count_review_items(
            status=status, label=label, state=state, disagreement=disagrees
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        db_obj.close()

    return jsonify(
        {
            "items": [_review_item_payload(item) for item in items],
            "count": len(items),
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )


@bp.route("/api/review/item")
def api_review_item():
    """Return one review item including its (inert) cached body text."""
    message_id = request.args.get("message_id")
    if not message_id:
        return jsonify({"error": "Missing required parameter: message_id"}), 400

    config = _get_config()

    from Mailroom.db import DB

    db_obj = DB(config.database_path)
    try:
        item = db_obj.get_review_item(message_id)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        db_obj.close()

    if not item:
        return jsonify({"error": f"Message not found: {message_id}"}), 404
    return jsonify(_review_item_payload(item, include_body=True))


@bp.route("/api/decisions", methods=["POST"])
def api_save_decision():
    """Create, update, or reopen the local decision for a message.

    Body: ``{"message_id", "status", "label_ids"?, "user_notes"?}``.
    ``status`` is one of pending/accepted/corrected/skipped. Accepting is
    refused for messages without a proposal or with an abstained proposal;
    correction (including an explicit empty selection) always remains allowed.
    """
    config = _get_config()

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid request: missing JSON body"}), 400

    message_id = data.get("message_id")
    status = data.get("status")
    if not message_id:
        return jsonify({"error": "Missing required field: message_id"}), 400
    if status not in ("pending", "accepted", "corrected", "skipped"):
        return jsonify({"error": f"Invalid status: {status!r}"}), 400

    raw_labels = data.get("label_ids") or []
    if not isinstance(raw_labels, list) or not all(isinstance(x, str) for x in raw_labels):
        return jsonify({"error": "label_ids must be a list of strings"}), 400

    user_notes = data.get("user_notes")
    if user_notes is not None and not isinstance(user_notes, str):
        return jsonify({"error": "user_notes must be a string or null"}), 400

    allowed = _label_id_set(config)

    from Mailroom.db import DB, DatabaseError

    db_obj = DB(config.database_path, allowed_labels=allowed)
    try:
        item = db_obj.get_review_item(message_id)
        if not item:
            return jsonify({"error": f"Message not found: {message_id}"}), 404

        account_id = item["account_id"]
        latest = db_obj.latest_proposal_for_message(message_id)

        if status == "accepted":
            if latest is None:
                return jsonify(
                    {"error": "Cannot accept: there is no current proposal to accept"}
                ), 400
            if latest.get("abstain"):
                return jsonify(
                    {
                        "error": "Cannot accept an abstained proposal; "
                        "correct the labels instead"
                    }
                ), 400
            proposed = latest.get("label_ids_parsed") or []
            unknown_proposed = [lid for lid in proposed if lid not in allowed]
            if unknown_proposed:
                # Do not silently record an empty accepted choice on config drift.
                return jsonify(
                    {
                        "error": "Proposal contains labels no longer configured: "
                        f"{unknown_proposed}. Correct the labels instead."
                    }
                ), 400
            chosen = proposed
        elif status == "corrected":
            chosen = raw_labels
        else:  # pending / skipped carry no selection
            chosen = []

        decision = db_obj.save_decision(
            account_id=account_id,
            message_id=message_id,
            status=status,
            label_ids=chosen,
            user_notes=user_notes,
            proposal_id=latest.get("id") if latest else None,
        )
    except DatabaseError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        db_obj.close()

    return jsonify({"decision_id": decision, "status": status, "label_ids": chosen})


@bp.route("/api/conflicts")
def api_conflicts():
    """Similar reviewed subjects that received different human label sets."""
    config = _get_config()
    from Mailroom.db import DB
    from Mailroom import pilot as pilot_module

    db_obj = DB(config.database_path)
    try:
        rows = pilot_module.list_reviewed_pairs(db_obj)
        found = pilot_module.find_label_conflicts(rows)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        db_obj.close()
    return jsonify(found)


@bp.route("/api/conflicts/resolve", methods=["POST"])
def api_resolve_conflict():
    """Apply one label set to every message in a conflict group (local only)."""
    config = _get_config()
    data = request.get_json(silent=True) or {}
    message_ids = data.get("message_ids") or []
    raw_labels = data.get("label_ids") or []
    if not isinstance(message_ids, list) or not all(isinstance(x, str) for x in message_ids):
        return jsonify({"error": "message_ids must be a list of strings"}), 400
    if not isinstance(raw_labels, list) or not all(isinstance(x, str) for x in raw_labels):
        return jsonify({"error": "label_ids must be a list of strings"}), 400
    allowed = _label_id_set(config)

    from Mailroom.db import DB, DatabaseError

    db_obj = DB(config.database_path, allowed_labels=allowed)
    updated = []
    try:
        # Resolve every id before writing so a missing message cannot leave a
        # partially-updated group.
        pending = []
        for message_id in message_ids:
            item = db_obj.get_review_item(message_id)
            if not item:
                return jsonify({"error": f"Message not found: {message_id}"}), 404
            latest = db_obj.latest_proposal_for_message(message_id)
            pending.append((item, latest))

        # One transaction for the whole group: all or nothing.
        for item, latest in pending:
            db_obj.save_decision(
                account_id=item["account_id"],
                message_id=item["message_id"],
                status="corrected",
                label_ids=raw_labels,
                proposal_id=latest.get("id") if latest else None,
                commit=False,
            )
            updated.append(item["message_id"])
        db_obj.conn.commit()
    except DatabaseError as e:
        db_obj.conn.rollback()
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        db_obj.conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db_obj.close()
    return jsonify({"updated": updated, "label_ids": raw_labels})


@bp.route("/api/export")
def api_export():
    """Export reviewed choices as CSV or JSON (no bodies, no reasons).

    Query parameters: ``format`` (csv|json, default csv), ``include_skipped``
    (1/true to include explicitly skipped messages).
    """
    config = _get_config()
    export_format = (request.args.get("format") or "csv").lower()
    if export_format not in ("csv", "json"):
        return jsonify({"error": f"Unsupported format: {export_format}"}), 400
    include_skipped = (request.args.get("include_skipped") or "").lower() in (
        "1",
        "true",
        "yes",
    )

    from Mailroom import export as export_module
    from Mailroom.db import DB

    db_obj = DB(config.database_path)
    try:
        decisions = db_obj.list_decisions_for_export(include_skipped=include_skipped)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        db_obj.close()

    records = export_module.build_export_records(decisions, _label_name_map(config))
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if export_format == "json":
        body = export_module.records_to_json(records)
        return Response(
            body,
            mimetype="application/json; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="mailroom-decisions-{today_str}.json"'
            },
        )

    body = export_module.records_to_csv(records)
    return Response(
        body,
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="mailroom-decisions-{today_str}.csv"'
        },
    )

