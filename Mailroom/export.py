"""Export reviewed label choices as CSV or JSON.

Exports contain identity and label choices only: no message bodies and no
model reasons. Spreadsheet-formula cells are neutralized so a leading
``=``/``+``/``-``/``@``/``|`` (or fullwidth lookalikes) cannot execute when
the CSV is opened.
"""

import csv
import io
import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

EXPORT_FIELDS = (
    "account_id",
    "message_id",
    "thread_id",
    "label_ids",
    "label_names",
    "status",
    "reviewed_at",
)

# Characters Excel/Sheets may treat as a formula trigger, even after
# leading whitespace is ignored. Includes ASCII, fullwidth lookalikes,
# and `|` (legacy DDE).
_CSV_FORMULA_TRIGGERS = frozenset("=+-@|＝＋－＠")
# ASCII whitespace, NBSP, BOM, and common Unicode space separators (Zs).
_CSV_LEADING_WHITESPACE = (
    " \t\r\n\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005"
    "\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)


def neutralize_csv_cell(value: Any) -> str:
    """Return a CSV-safe string; prefix spreadsheet formula triggers.

    A value is neutralized when its first non-whitespace character is a
    formula trigger, or when it starts with a bare tab/CR (which spreadsheet
    importers can interpret specially). A leading UTF-8 BOM is stripped so
    it cannot hide a trigger.
    """
    text = "" if value is None else str(value)
    while text.startswith("\ufeff"):
        text = text[1:]
    if not text:
        return text
    if text[0] in ("\t", "\r"):
        return "'" + text
    stripped = text.lstrip(_CSV_LEADING_WHITESPACE)
    if stripped and stripped[0] in _CSV_FORMULA_TRIGGERS:
        return "'" + text
    return text


def build_export_records(
    decisions: Iterable[Dict[str, Any]],
    label_name_by_id: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """Shape database decision rows into export records.

    ``decisions`` are the rows returned by ``DB.list_decisions_for_export``.
    ``label_name_by_id`` maps label ids to human names.
    """
    label_name_by_id = label_name_by_id or {}
    records: List[Dict[str, Any]] = []
    for decision in decisions:
        label_ids = list(decision.get("label_ids") or [])
        records.append(
            {
                "account_id": decision.get("account_id"),
                "message_id": decision.get("gmail_message_id"),
                "thread_id": decision.get("thread_id"),
                "label_ids": label_ids,
                "label_names": [label_name_by_id.get(lid, lid) for lid in label_ids],
                "status": decision.get("status"),
                "reviewed_at": decision.get("reviewed_at"),
            }
        )
    return records


def records_to_csv(records: Iterable[Dict[str, Any]]) -> str:
    """Serialize export records to CSV with formula injection neutralized."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(EXPORT_FIELDS)
    for record in records:
        row = []
        for field in EXPORT_FIELDS:
            value = record.get(field)
            if isinstance(value, list):
                value = ",".join(str(v) for v in value)
            row.append(neutralize_csv_cell(value))
        writer.writerow(row)
    return buffer.getvalue()


def records_to_json(records: Iterable[Dict[str, Any]]) -> str:
    """Serialize export records to a JSON document."""
    records = list(records)
    payload = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "count": len(records),
        "decisions": records,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
