"""Step 5 pilot metrics from human review. Ground truth is human decisions only.

Never includes message bodies. Optional local-model prose uses stats JSON only.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from Mailroom.classification import ClassificationError, TabbyClient

from Mailroom.config import normalize_label_id


def _norm_ids(raw: Any) -> List[str]:
    from Mailroom.db import _parse_json_list

    ids = _parse_json_list(raw) if not isinstance(raw, list) else list(raw)
    out = []
    for lid in ids:
        lid = normalize_label_id(str(lid))
        if lid and lid not in out:
            out.append(lid)
    return out


def list_reviewed_pairs(db, prefer_latest: bool = False) -> List[Dict[str, Any]]:
    """Human decisions joined to model proposals.

    By default a decision is paired with the proposal it was based on
    (``decisions.proposal_id``), falling back to the latest proposal. With
    ``prefer_latest=True`` the newest proposal for the message is always used,
    so a changed model or prompt can be scored against frozen human labels.
    """
    if prefer_latest:
        model_expr = "lp.label_ids"
        abstain_expr = "COALESCE(lp.abstain, 0)"
    else:
        model_expr = "COALESCE(p.label_ids, lp.label_ids)"
        abstain_expr = "COALESCE(p.abstain, lp.abstain, 0)"
    cursor = db.conn.cursor()
    cursor.execute(
        f"""
        SELECT d.message_id,
               d.status,
               d.label_ids AS human_json,
               d.updated_at,
               m.subject AS subject,
               m.sender_email AS sender_email,
               {model_expr} AS model_json,
               {abstain_expr} AS abstain
        FROM decisions d
        LEFT JOIN messages m ON m.id = d.message_id
        LEFT JOIN proposals p ON p.id = d.proposal_id
        LEFT JOIN proposals lp ON lp.rowid = (
            SELECT p2.rowid FROM proposals p2
            WHERE p2.message_id = d.message_id
            ORDER BY p2.rowid DESC LIMIT 1
        )
        WHERE d.status IN ('accepted', 'corrected', 'skipped')
        ORDER BY d.message_id
        """
    )
    rows = []
    for row in cursor.fetchall():
        rows.append(
            {
                "message_id": row["message_id"],
                "status": row["status"],
                "human": _norm_ids(row["human_json"]),
                "model": _norm_ids(row["model_json"]),
                "abstain": bool(row["abstain"]),
                "updated_at": row["updated_at"],
                "subject": row["subject"] or "",
                "sender_email": row["sender_email"] or "",
            }
        )
    return rows


_SUBJ_PREFIX = re.compile(r"^(re|fw|fwd|aw|wg)\s*:\s*", re.I)


def normalize_subject(subject: str) -> str:
    text = (subject or "").strip().lower()
    while True:
        nxt = _SUBJ_PREFIX.sub("", text)
        if nxt == text:
            break
        text = nxt.strip()
    return re.sub(r"\s+", " ", text)


def find_label_conflicts(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Same or very similar subjects with different human label sets."""
    scored = [r for r in rows if r["status"] in ("accepted", "corrected")]
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for row in scored:
        key = normalize_subject(row.get("subject") or "")
        if len(key) < 8:
            sender = (row.get("sender_email") or "").lower()
            key = f"{sender}::{key}" if key else ""
        if not key:
            continue
        # Treat long subjects as similar if they share the first 48 chars.
        if len(key) > 48:
            key = key[:48]
        groups.setdefault(key, []).append(row)

    conflicts = []
    for key, members in groups.items():
        if len(members) < 2:
            continue
        sets = {tuple(sorted(m["human"])) for m in members}
        if len(sets) < 2:
            continue
        conflicts.append(
            {
                "phrase": key,
                "n": len(members),
                "label_sets": [list(s) for s in sorted(sets)],
                "subjects": list({m.get("subject") or "" for m in members}),
                "members": [
                    {
                        "message_id": m.get("message_id"),
                        "subject": m.get("subject") or "",
                        "label_ids": list(m.get("human") or []),
                    }
                    for m in members
                ],
            }
        )
    conflicts.sort(key=lambda c: (-int(str(c["n"])), c["phrase"]))
    return {
        "n_groups": len(conflicts),
        "n_messages": sum(c["n"] for c in conflicts),
        "groups": conflicts,
    }


def refresh_config_examples(config, db, max_examples: int = 6) -> int:
    """Add reviewed subjects as examples on matching config labels. Local only."""
    rows = list_reviewed_pairs(db)
    by_label: Dict[str, List[str]] = {}
    for row in rows:
        if row["status"] not in ("accepted", "corrected"):
            continue
        subject = (row.get("subject") or "").strip()
        if not subject:
            continue
        for lid in row["human"]:
            bucket = by_label.setdefault(lid, [])
            if subject not in bucket:
                bucket.append(subject)
    added = 0
    for lab in config._config.get("labels") or []:
        extras = by_label.get(lab["id"]) or []
        examples = list(lab.get("examples") or [])
        for subject in extras:
            if subject in examples:
                continue
            examples.append(subject)
            added += 1
            if len(examples) >= max_examples:
                break
        lab["examples"] = examples[:max_examples]
    config.save()
    return added


def split_holdout(rows: List[Dict[str, Any]], fraction: float = 0.2) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Deterministic holdout: last ``fraction`` of sorted message_id."""
    if not rows or fraction <= 0:
        return list(rows), []
    ordered = sorted(rows, key=lambda r: r["message_id"])
    n_hold = max(1, int(round(len(ordered) * fraction))) if len(ordered) >= 5 else 0
    if n_hold == 0:
        return ordered, []
    return ordered[:-n_hold], ordered[-n_hold:]


def _label_stats(rows: List[Dict[str, Any]], allowed: List[str]) -> Dict[str, Any]:
    scored = [r for r in rows if r["status"] in ("accepted", "corrected")]
    skipped = [r for r in rows if r["status"] == "skipped"]
    accepted = [r for r in rows if r["status"] == "accepted"]
    corrected = [r for r in rows if r["status"] == "corrected"]
    abstained = [r for r in scored if r["abstain"]]
    per: Dict[str, Dict[str, Any]] = {}
    for lid in allowed:
        tp = fp = fn = 0
        for r in scored:
            in_m = lid in r["model"]
            in_h = lid in r["human"]
            if in_m and in_h:
                tp += 1
            elif in_m and not in_h:
                fp += 1
            elif in_h and not in_m:
                fn += 1
        pred = tp + fp
        truth = tp + fn
        precision = (tp / pred) if pred else None
        recall = (tp / truth) if truth else None
        per[lid] = {
            "true_positive": tp,
            "false_positive": fp,
            "false_negative": fn,
            "predicted": pred,
            "human": truth,
            "precision": precision,
            "recall": recall,
        }
    n_scored = len(scored)
    with_label = sum(1 for r in scored if r["human"])
    return {
        "n": len(rows),
        "n_scored": n_scored,
        "n_accepted": len(accepted),
        "n_corrected": len(corrected),
        "n_skipped": len(skipped),
        "n_abstain_model": len(abstained),
        "accept_rate_among_scored": (len(accepted) / n_scored) if n_scored else None,
        "correct_rate_among_scored": (len(corrected) / n_scored) if n_scored else None,
        "human_nonempty_rate": (with_label / n_scored) if n_scored else None,
        "per_label": per,
    }


def compute_pilot_metrics(
    db,
    allowed_label_ids: List[str],
    holdout_fraction: float = 0.2,
    prefer_latest: bool = False,
) -> Dict[str, Any]:
    rows = list_reviewed_pairs(db, prefer_latest=prefer_latest)
    train, holdout = split_holdout(rows, holdout_fraction)
    conflicts = find_label_conflicts(rows)
    return {
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "ground_truth": "human decisions (accepted/corrected/skipped)",
        "proposal_basis": "latest" if prefer_latest else "reviewed_basis",
        "latency_seconds": None,
        "latency_note": "Per-message inference latency was not recorded.",
        "n_reviewed_total": len(rows),
        "holdout_fraction": holdout_fraction,
        "train": _label_stats(train, allowed_label_ids),
        "holdout": _label_stats(holdout, allowed_label_ids),
        "all_reviewed": _label_stats(rows, allowed_label_ids),
        "conflicts": {
            "n_groups": conflicts["n_groups"],
            "n_messages": conflicts["n_messages"],
            "groups": conflicts["groups"],
        },
        "unreviewed_not_used": True,
    }


def stats_to_markdown(metrics: Dict[str, Any], model_id: str, include_conflict_phrases: bool = False) -> str:
    """Deterministic report from numbers. Bodies never included."""

    def fmt(x):
        if x is None:
            return "undefined (no cases)"
        if isinstance(x, float):
            return f"{x:.2f}"
        return str(x)

    lines = [
        "# Mailroom V1 pilot report",
        "",
        f"Computed at {metrics.get('computed_at')}.",
        f"Local model id: `{model_id}`.",
        "Ground truth: **human review decisions only**. Unreviewed mail is not used.",
        "No message bodies or subjects are included.",
        "Proposals scored: "
        + (
            "**latest per message** (reclassification; human labels frozen)."
            if metrics.get("proposal_basis") == "latest"
            else "**the proposal the reviewer saw** (decision basis)."
        ),
        "",
        "## Sample",
        "",
        f"- Reviewed (accepted/corrected/skipped): **{metrics['n_reviewed_total']}**",
        f"- Held-out slice: **{metrics['holdout_fraction']:.0%}** of reviewed "
        f"({metrics['holdout']['n']} messages), last by sorted message id",
        f"- Train / calibration slice: {metrics['train']['n']} messages",
        f"- Inference latency: {metrics.get('latency_note')}",
        "",
        "## Agreement with the reviewer (all reviewed)",
        "",
        f"- Accepted as-is: {metrics['all_reviewed']['n_accepted']}",
        f"- Corrected: {metrics['all_reviewed']['n_corrected']}",
        f"- Skipped: {metrics['all_reviewed']['n_skipped']}",
        f"- Accept rate among scored: {fmt(metrics['all_reviewed']['accept_rate_among_scored'])}",
        f"- Model abstain among scored: {metrics['all_reviewed']['n_abstain_model']}",
        "",
        "## Per-label precision / recall (all reviewed, scored only)",
        "",
        "| Label | Human | Predicted | Precision | Recall |",
        "|---|---:|---:|---:|---:|",
    ]
    for lid, s in metrics["all_reviewed"]["per_label"].items():
        lines.append(
            f"| `{lid}` | {s['human']} | {s['predicted']} | {fmt(s['precision'])} | {fmt(s['recall'])} |"
        )
    lines += [
        "",
        "## Held-out (not for tuning)",
        "",
        f"- n={metrics['holdout']['n']}, accepted={metrics['holdout']['n_accepted']}, "
        f"corrected={metrics['holdout']['n_corrected']}",
        f"- Accept rate: {fmt(metrics['holdout']['accept_rate_among_scored'])}",
        "",
        "## Label conflicts (similar phrases, different human labels)",
        "",
        f"- Conflict groups: **{metrics.get('conflicts', {}).get('n_groups', 0)}**",
        f"- Messages in those groups: **{metrics.get('conflicts', {}).get('n_messages', 0)}**",
        "",
    ]
    if include_conflict_phrases:
        for group in metrics.get("conflicts", {}).get("groups") or []:
            sets = " vs ".join("+".join(s) or "(none)" for s in group["label_sets"])
            phrase = group.get("phrase") or ""
            lines.append(f"- `{phrase}` ({group['n']} msgs): {sets}")
        if not metrics.get("conflicts", {}).get("groups"):
            lines.append("- None.")
        lines.append("")
    else:
        lines.append("Phrases are omitted from the tracked report; see the local conflicts JSON.")
        lines.append("")
    lines += [
        "## Limitations",
        "",
        "- Gmail labels are **not** applied. V1 is read-only.",
        "- Live Gmail link clicks and browser inertness were not automated here.",
        "- Precision is undefined when the model never predicted a label.",
        "- Agreement is capped by the saved labels themselves; where the reviewer's "
        "decisions are internally inconsistent, no model can score well against them.",
        "- Do not treat these rates as a product accuracy target.",
        "",
    ]
    return "\n".join(lines)


def local_model_narrative(client: TabbyClient, metrics: Dict[str, Any]) -> str:
    """Ask the local model to comment on stats only. Must not invent counts."""
    safe = dict(metrics)
    conflicts = dict(metrics.get("conflicts") or {})
    safe["conflicts"] = {
        "n_groups": conflicts.get("n_groups", 0),
        "n_messages": conflicts.get("n_messages", 0),
    }
    prompt = f"""You write a short pilot commentary for a local email-labeling tool.
Use ONLY these numbers. Do not invent counts, do not quote email text, do not
recommend sending mail to a cloud model. If a metric is null, say it is undefined.
Mention conflict groups if n_groups > 0 (same/similar subject, different human labels).

Return plain markdown (no JSON), at most 12 sentences: what looks strong, what
the reviewer corrected often, conflicts, and that Gmail writes are out of scope.

STATS:
{json.dumps(safe, indent=2, default=str)}
"""
    raw = client._generate(prompt, max_tokens=500)
    from Mailroom.classification import _extract_completion_text

    text = _extract_completion_text(raw).strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("markdown"):
            text = text[8:]
        text = text.strip()
    return text


def build_pilot_report(
    metrics: Dict[str, Any],
    model_id: str,
    client: Optional[TabbyClient] = None,
    include_conflict_phrases: bool = False,
) -> str:
    base = stats_to_markdown(metrics, model_id, include_conflict_phrases=include_conflict_phrases)
    if client is None:
        return base
    try:
        narrative = local_model_narrative(client, metrics)
    except (ClassificationError, Exception) as e:
        narrative = f"(Local model commentary skipped: {e})"
    return base + "\n## Local model commentary (stats only)\n\n" + narrative + "\n"
