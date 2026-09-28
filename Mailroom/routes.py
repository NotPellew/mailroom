"""Flask routes for Mailroom review page."""

import io
import json
import logging
import re
import sqlite3
import threading
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


_SAFE_MODEL_RE = re.compile(
    r"^[a-zA-Z0-9_]+([.-][a-zA-Z0-9_]+)*(/[a-zA-Z0-9_]+([.-][a-zA-Z0-9_]+)*)*(:[a-zA-Z0-9_]+([.-][a-zA-Z0-9_]+)*)?$"
)


@bp.route("/api/ollama/status")
def api_ollama_status():
    """Check status of local AI backend and model availability."""
    config = _get_config()
    provider = config.model_provider
    endpoint = config.model_endpoint
    model_id = config.model_id

    if not is_loopback_url(endpoint):
        return jsonify({"error": "Model endpoint is not a loopback URL"}), 400

    from Mailroom.classification import detect_provider, is_model_installed, ollama_base, probe_model_endpoint
    import requests

    eff_provider = detect_provider(endpoint, explicit_provider=provider)

    # If provider is not ollama (e.g. tabby or openai):
    if eff_provider in ("tabby", "openai"):
        try:
            probe_model_endpoint(endpoint, provider=eff_provider, timeout=3.0)
            return jsonify({
                "provider": eff_provider,
                "running": True,
                "model_installed": True,
                "ready": True,
                "endpoint": endpoint,
                "model": model_id,
            })
        except Exception as e:
            return jsonify({
                "provider": eff_provider,
                "running": False,
                "model_installed": False,
                "ready": False,
                "endpoint": endpoint,
                "model": model_id,
                "error": str(e),
            })

    base = ollama_base(endpoint)
    session = requests.Session()
    session.trust_env = False
    session.proxies = {"http": None, "https": None}

    try:
        resp = session.get(f"{base}/api/tags", timeout=3.0)
        if resp.status_code == 200:
            data = resp.json()
            models = data.get("models") or []
            available = [
                str(m.get("name") or m.get("model"))
                for m in models
                if isinstance(m, dict) and (m.get("name") or m.get("model"))
            ]
            installed = is_model_installed(model_id, available)
            return jsonify({
                "provider": "ollama",
                "running": True,
                "model_installed": installed,
                "ready": installed,
                "model": model_id,
                "endpoint": endpoint,
                "available_models": available,
            })
        else:
            return jsonify({
                "provider": "ollama",
                "running": False,
                "model_installed": False,
                "ready": False,
                "model": model_id,
                "endpoint": endpoint,
                "error": f"Ollama returned HTTP {resp.status_code}",
            })
    except Exception as e:
        return jsonify({
            "provider": "ollama",
            "running": False,
            "model_installed": False,
            "ready": False,
            "model": model_id,
            "endpoint": endpoint,
            "error": str(e),
        })
    finally:
        session.close()


@bp.route("/api/ollama/pull", methods=["POST"])
def api_ollama_pull():
    """Pull an Ollama model streaming SSE progress events."""
    config = _get_config()
    provider = config.model_provider
    endpoint = config.model_endpoint
    if not is_loopback_url(endpoint):
        return jsonify({"error": "Model endpoint is not a loopback URL"}), 400

    from Mailroom.classification import detect_provider, ollama_base
    import requests

    eff_provider = detect_provider(endpoint, explicit_provider=provider)
    if eff_provider != "ollama":
        return jsonify({"error": "Model download is only supported when using Ollama"}), 400

    data = request.get_json(silent=True) or {}
    model_name = data.get("model") or config.model_id
    if not isinstance(model_name, str):
        return jsonify({"error": "Invalid model parameter"}), 400

    model_name = model_name.strip()
    if not model_name or len(model_name) > 128 or ".." in model_name or not _SAFE_MODEL_RE.match(model_name):
        return jsonify({"error": f"Invalid model name: '{model_name}'"}), 400

    base = ollama_base(endpoint)

    def generate():
        session = requests.Session()
        session.trust_env = False
        session.proxies = {"http": None, "https": None}
        try:
            with session.post(
                f"{base}/api/pull",
                json={"name": model_name, "stream": True},
                stream=True,
                timeout=(5.0, None),
            ) as resp:
                if resp.status_code != 200:
                    yield f"data: {json.dumps({'error': f'Ollama error HTTP {resp.status_code}'})}\n\n"
                    return
                for line in resp.iter_lines():
                    if line:
                        decoded = line.decode("utf-8", errors="replace")
                        yield f"data: {decoded}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
        finally:
            session.close()

    return Response(
        generate(),
        mimetype="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@bp.route("/api/system/health")
def api_system_health():
    """Visual system health dashboard metrics."""
    config = _get_config()
    db_path = config.database_path
    st = get_status(db_path)
    db_status = "ready" if st.get("db_initialized") else "not_initialized"
    db_info = {
        "status": db_status,
        "path": str(db_path),
        "messages": st.get("messages", 0),
        "decisions": st.get("reviewed", 0),
        "unclassified": st.get("unclassified", 0),
    }

    endpoint = config.model_endpoint
    provider = config.model_provider
    model_id = config.model_id

    from Mailroom.classification import (
        detect_provider,
        probe_model_endpoint,
        is_model_installed,
        ClassificationError,
    )

    if not is_loopback_url(endpoint):
        ai_info = {
            "status": "offline",
            "provider": provider,
            "model": model_id,
            "endpoint": endpoint,
            "available_models": [],
            "error": "Model endpoint is not a loopback URL",
        }
    else:
        eff_provider = detect_provider(endpoint, explicit_provider=provider)
        probe_timeout = min(getattr(config, "timeout", 30.0), 3.0)
        try:
            available_models = probe_model_endpoint(endpoint, timeout=probe_timeout, provider=eff_provider)
            if eff_provider == "ollama":
                installed = is_model_installed(model_id, available_models)
                ai_status = "ready" if installed else "action_needed"
            else:
                ai_status = "ready"
            ai_info = {
                "status": ai_status,
                "provider": eff_provider,
                "model": model_id,
                "endpoint": endpoint,
                "available_models": available_models,
                "error": None,
            }
        except (ClassificationError, Exception) as exc:
            ai_info = {
                "status": "offline",
                "provider": eff_provider,
                "model": model_id,
                "endpoint": endpoint,
                "available_models": [],
                "error": str(exc),
            }

    from Mailroom.gmail import check_gmail_dependencies, _read_last_email
    gmail_deps_ok, _ = check_gmail_dependencies()
    stored_account = None
    if gmail_deps_ok:
        try:
            stored_account = _read_last_email(config)
        except Exception:
            stored_account = None

    gmail_info = {
        "status": "connected" if stored_account else "local_only",
        "account": stored_account,
        "dependencies_installed": gmail_deps_ok,
    }

    onboarding_info = {
        "completed": bool(config.get("onboarding_completed", False)),
        "profile": config.get("hardware_profile"),
        "taxonomy": config.get("taxonomy_archetype"),
    }

    return jsonify({
        "database": db_info,
        "ai": ai_info,
        "gmail": gmail_info,
        "onboarding": onboarding_info,
    })


@bp.route("/api/config/setup", methods=["POST"])
def api_config_setup():
    """First-launch onboarding setup endpoint."""
    config = _get_config()
    data = request.get_json(silent=True) or {}

    from Mailroom.config import (
        HARDWARE_PROFILES,
        Config,
        single_label_taxonomy,
        ConfigError,
    )
    from Mailroom.db import DB

    if data.get("skip"):
        config._config["onboarding_completed"] = True
        try:
            config.validate()
            config.save()
        except ConfigError as err:
            return jsonify({"error": str(err)}), 400
        try:
            DB(config.database_path).close()
        except Exception as e:
            logger.warning("Could not initialize database on skip: %s", e)
        return jsonify({"status": "success", "skipped": True})

    profile = data.get("profile", "standard")
    if profile not in HARDWARE_PROFILES:
        return jsonify({
            "error": f"Invalid profile: '{profile}'. Must be one of: {list(HARDWARE_PROFILES.keys())}"
        }), 400

    taxonomy = data.get("taxonomy", "standard")
    if taxonomy not in ("standard", "single-label"):
        return jsonify({
            "error": f"Invalid taxonomy: '{taxonomy}'. Must be 'standard' or 'single-label'"
        }), 400

    if taxonomy == "single-label":
        target_label = data.get("target_label", "Rechnungen")
        if not isinstance(target_label, str) or not target_label.strip() or len(target_label.strip()) > 64:
            return jsonify({"error": "target_label must be a non-empty string under 64 characters"}), 400
        target_label = target_label.strip()
        try:
            labels = single_label_taxonomy(target_label)
        except ConfigError as err:
            return jsonify({"error": str(err)}), 400
        scan_query = Config.DOCUMENTS_ATTACHMENT_QUERY
    else:
        labels = Config.DEFAULT_LABELS
        scan_query = Config.DEFAULT_SCAN_QUERY

    hw_profile = HARDWARE_PROFILES[profile]
    config._config["model"] = {
        "endpoint": hw_profile["endpoint"],
        "id": hw_profile["id"],
        "provider": hw_profile["provider"],
    }
    config._config["labels"] = labels
    config._config["scan_query"] = scan_query
    config._config["onboarding_completed"] = True
    config._config["hardware_profile"] = profile
    config._config["taxonomy_archetype"] = taxonomy

    try:
        config.validate()
        config.save()
    except ConfigError as err:
        return jsonify({"error": str(err)}), 400

    try:
        DB(config.database_path).close()
    except Exception as e:
        logger.warning("Could not initialize database on setup: %s", e)

    return jsonify({
        "status": "success",
        "profile": profile,
        "taxonomy": taxonomy,
        "labels_count": len(labels),
    })


# --- Guided In-App Gmail Connection & Label Sync ---

_GMAIL_AUTH_LOCK = threading.Lock()


def _compute_apply_plan(config):
    """Compute planned label additions and removals for Gmail messages."""
    from Mailroom.db import DB

    db_obj = DB(config.database_path)
    try:
        targets = db_obj.list_gmail_apply_targets()
        snapshots = {row["message_id"]: row["label_ids"] for row in db_obj.list_applied_labels()}
    finally:
        db_obj.close()

    known = {lab["id"] for lab in config.labels}
    planned = []
    needed_labels = set()

    for target in targets:
        desired = [lid for lid in target["label_ids"] if lid in known]
        previous = snapshots.get(target["message_id"]) or []
        previous_valid = [lid for lid in previous if lid in known]
        add = [lid for lid in desired if lid not in previous_valid]
        remove = [lid for lid in previous_valid if lid not in desired]
        if add or remove:
            needed_labels.update(desired)
            planned.append({
                "message_id": target["message_id"],
                "gmail_message_id": target["gmail_message_id"],
                "subject": target.get("subject") or "(no subject)",
                "sender": target.get("sender") or "",
                "account_id": target["account_id"],
                "desired": desired,
                "labels_to_add": add,
                "labels_to_remove": remove,
            })

    return planned, sorted(needed_labels)


@bp.route("/api/gmail/status")
def api_gmail_status():
    """Report Gmail dependency, credentials, and connection status."""
    config = _get_config()
    from Mailroom.gmail import (
        check_gmail_dependencies,
        _load_client_secrets,
        _read_last_email,
        _load_credentials,
        GMAIL_MODIFY_SCOPE,
    )

    deps_ok, _ = check_gmail_dependencies()
    has_secrets = _load_client_secrets(config.config_dir) is not None
    stored_email = None
    has_creds = False

    if deps_ok:
        try:
            stored_email = _read_last_email(config)
            if stored_email:
                creds_dict = _load_credentials(stored_email)
                if creds_dict:
                    scopes = creds_dict.get("scopes") or []
                    if isinstance(scopes, str):
                        scopes = scopes.split()
                    if GMAIL_MODIFY_SCOPE in scopes:
                        has_creds = True
        except Exception:
            stored_email = None
            has_creds = False

    connected = bool(stored_email and has_creds)
    return jsonify({
        "dependencies_installed": deps_ok,
        "credentials_uploaded": has_secrets,
        "connected": connected,
        "account": stored_email if connected else None,
        "mode": "gmail" if connected else "local",
    })


@bp.route("/api/gmail/credentials", methods=["POST"])
def api_gmail_credentials():
    """Upload Google Cloud OAuth client credentials (credentials.json)."""
    config = _get_config()

    # Limit payload size to 256 KB
    if request.content_length and request.content_length > 256 * 1024:
        return jsonify({"error": "File exceeds maximum size of 256 KB"}), 400

    raw_data = None
    if "file" in request.files:
        upload = request.files["file"]
        content = upload.read()
        if len(content) > 256 * 1024:
            return jsonify({"error": "File exceeds maximum size of 256 KB"}), 400
        try:
            raw_data = json.loads(content.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            return jsonify({"error": f"Invalid JSON in credentials file: {e}"}), 400
    else:
        raw_data = request.get_json(silent=True)

    if not isinstance(raw_data, dict):
        return jsonify({"error": "Credentials must be a JSON object"}), 400

    if "web" in raw_data and "installed" not in raw_data:
        return jsonify({
            "error": "OAuth credentials must be created as a 'Desktop app' in Google Cloud Console, not 'Web application'."
        }), 400

    if "installed" not in raw_data or not isinstance(raw_data["installed"], dict):
        return jsonify({
            "error": "Credentials JSON missing 'installed' desktop client block."
        }), 400

    installed = raw_data["installed"]
    client_id = installed.get("client_id")
    client_secret = installed.get("client_secret")
    if not isinstance(client_id, str) or not client_id.strip():
        return jsonify({"error": "Missing or invalid client_id in credentials"}), 400
    if not isinstance(client_secret, str) or not client_secret.strip():
        return jsonify({"error": "Missing or invalid client_secret in credentials"}), 400

    target_path = Path(config.config_dir) / "credentials.json"
    try:
        config.config_dir.mkdir(parents=True, exist_ok=True)
        with open(target_path, "w", encoding="utf-8") as f:
            json.dump(raw_data, f, indent=2)
        target_path.chmod(0o600)
    except Exception as e:
        return jsonify({"error": f"Failed to save credentials: {e}"}), 500

    safe_id = client_id.split("-")[0] + "...apps.googleusercontent.com"
    return jsonify({"status": "success", "client_id": safe_id})


@bp.route("/api/gmail/auth", methods=["POST"])
def api_gmail_auth():
    """Trigger Google OAuth authorization flow in local browser."""
    config = _get_config()

    if not _GMAIL_AUTH_LOCK.acquire(blocking=False):
        return jsonify({"error": "Authentication is already in progress in your browser."}), 409

    try:
        from Mailroom.gmail import (
            check_gmail_dependencies,
            _load_client_secrets,
            authenticate,
            GmailError,
        )

        deps_ok, missing = check_gmail_dependencies()
        if not deps_ok:
            return jsonify({
                "error": f"Gmail dependencies not installed: {', '.join(missing)}"
            }), 400

        if not _load_client_secrets(config.config_dir):
            return jsonify({
                "error": "Desktop OAuth credentials not uploaded yet. Upload credentials.json first."
            }), 400

        try:
            email = authenticate(config, reauth=True)
            return jsonify({"status": "success", "account": email})
        except GmailError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": f"Authentication failed: {e}"}), 500
    finally:
        _GMAIL_AUTH_LOCK.release()


@bp.route("/api/gmail/disconnect", methods=["POST"])
def api_gmail_disconnect():
    """Disconnect Gmail account, clearing keyring and marker."""
    config = _get_config()
    from Mailroom.gmail import _read_last_email, clear_stored_auth, _last_email_path

    try:
        email = _read_last_email(config)
        if email:
            clear_stored_auth(email)
        _last_email_path(config).unlink(missing_ok=True)
    except Exception as e:
        logger.warning("Error during Gmail disconnect: %s", e)

    return jsonify({"status": "success", "connected": False})


@bp.route("/api/gmail/scan", methods=["POST"])
def api_gmail_scan():
    """Fetch recent messages safely from Gmail and upsert into database."""
    config = _get_config()
    from Mailroom.gmail import get_gmail_service, fetch_bounded_sample, GmailError
    from Mailroom.db import DB

    data = request.get_json(silent=True) or {}
    try:
        limit = int(data.get("limit", 20))
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 100))

    try:
        service, email = get_gmail_service(config)
    except GmailError as e:
        return jsonify({"error": str(e)}), 400

    db_obj = DB(config.database_path)
    new_count = 0
    try:
        account_id = db_obj.get_or_create_account(
            email=email,
            name=email,
            is_local=False,
        )
        skip_ids = set(db_obj.list_cached_gmail_ids(account_id))

        def on_msg(decoded):
            nonlocal new_count
            db_obj.upsert_message(account_id, decoded)
            new_count += 1

        _, sample_time = fetch_bounded_sample(
            service,
            query=config.scan_query,
            limit=limit,
            skip_ids=skip_ids,
            on_message=on_msg,
        )
        db_obj.record_scan_metadata(account_id, sample_time, config.scan_query)
    finally:
        db_obj.close()

    return jsonify({
        "status": "success",
        "fetched": limit,
        "new": new_count,
        "account": email,
    })


@bp.route("/api/gmail/apply/preview")
def api_gmail_apply_preview():
    """Return explicit preview of pending Gmail label changes."""
    config = _get_config()
    planned, needed = _compute_apply_plan(config)

    items = [
        {
            "message_id": p["message_id"],
            "gmail_message_id": p["gmail_message_id"],
            "subject": p["subject"],
            "sender": p["sender"],
            "labels_to_add": p["labels_to_add"],
            "labels_to_remove": p["labels_to_remove"],
        }
        for p in planned
    ]

    return jsonify({
        "total_messages": len(items),
        "distinct_labels": needed,
        "items": items,
        "safety_guarantee": "0 messages will be archived or deleted. Only label additions and removals will be performed.",
    })


@bp.route("/api/gmail/apply", methods=["POST"])
def api_gmail_apply():
    """Apply planned label additions and removals to Gmail."""
    config = _get_config()
    from Mailroom.gmail import (
        get_gmail_service,
        list_user_labels,
        ensure_label,
        modify_message_labels,
        GmailError,
    )
    from Mailroom.db import DB

    planned, needed = _compute_apply_plan(config)
    if not planned:
        return jsonify({"status": "success", "applied": 0, "failed": 0})

    try:
        service, email = get_gmail_service(config)
    except GmailError as e:
        return jsonify({"error": str(e)}), 400

    label_map = list_user_labels(service)
    label_ids = {name: ensure_label(service, name, label_map) for name in sorted(needed)}

    db_obj = DB(config.database_path)
    applied = failed = 0
    try:
        for item in planned:
            gmail_id = item["gmail_message_id"]
            add_ids = [label_ids[n] for n in item["labels_to_add"] if n in label_ids]
            remove_ids = [label_ids[n] for n in item["labels_to_remove"] if n in label_ids]
            try:
                modify_message_labels(
                    service,
                    gmail_id,
                    add_label_ids=add_ids,
                    remove_label_ids=remove_ids,
                )
                db_obj.record_applied_labels(item["account_id"], item["message_id"], item["desired"])
                applied += 1
            except GmailError as e:
                failed += 1
                logger.warning("Failed to modify Gmail message %s: %s", gmail_id, e)
    finally:
        db_obj.close()

    return jsonify({
        "status": "success",
        "applied": applied,
        "failed": failed,
    })


@bp.route("/api/gmail/sync-labels", methods=["POST"])
def api_gmail_sync_labels():
    """Read Gmail labels back for applied mail; record user edits as decisions."""
    config = _get_config()
    from Mailroom.gmail import (
        get_gmail_service,
        list_user_labels,
        fetch_message_label_ids,
        GmailError,
    )
    from Mailroom.db import DB

    try:
        service, email = get_gmail_service(config)
    except GmailError as e:
        return jsonify({"error": str(e)}), 400

    label_map = list_user_labels(service)
    id_to_name = {lid: name for name, lid in label_map.items()}
    known = {lab["id"] for lab in config.labels}

    db_obj = DB(config.database_path, allowed_labels=config.get_label_ids())
    unchanged = corrected = errors = 0
    try:
        for snap in db_obj.list_applied_labels():
            gmail_id = snap["gmail_message_id"]
            previous = snap["label_ids"]
            try:
                current_ids = fetch_message_label_ids(service, gmail_id)
            except GmailError:
                errors += 1
                continue
            current = [
                id_to_name[lid]
                for lid in current_ids
                if lid in id_to_name and id_to_name[lid] in known
            ]
            if sorted(current) == sorted(previous):
                unchanged += 1
                continue
            db_obj.save_decision(snap["account_id"], snap["message_id"], "corrected", current)
            db_obj.record_applied_labels(snap["account_id"], snap["message_id"], current)
            corrected += 1
    finally:
        db_obj.close()

    return jsonify({
        "status": "success",
        "unchanged": unchanged,
        "corrected": corrected,
        "errors": errors,
    })



