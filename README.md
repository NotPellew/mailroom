# Mailroom

Mailroom is a local-first tool for suggesting, reviewing, and correcting labels
for your mail. Classify local `.eml` files (or optionally a Gmail inbox) with a
local model, review and correct the suggestions in a loopback web UI, then export
the decisions as CSV or JSON. Inference stays on your machine.

Gmail synchronization is strictly optional. Without OAuth credentials the whole
application — ingest, classification, web review, and export — works offline.
Gmail is read-only apart from creating labels and adding/removing labels on
messages, and those writes only happen when you run an explicit command with
`--apply`. Nothing is ever archived, deleted, trashed, marked spam, or sent, and
retention is a label only, with no automatic deletion.

## Quick start (local-first, no Gmail account needed)

Prerequisites: Python **3.12+** and a local Ollama installation. Follow the
official [Ollama Quickstart](https://docs.ollama.com/quickstart) to install it,
start the app or `ollama serve`, then pull the default model:

```bash
ollama pull qwen2.5:7b
```

Then install Mailroom and create a config. Using a virtual environment avoids
the externally-managed-environment error on recent Debian/Homebrew Python. With
no flags, `create-config` uses the Ollama `standard` profile (`qwen2.5:7b` on
`http://127.0.0.1:11434`); the `mailroom` console script and `python -m Mailroom`
are equivalent:

```bash
python3.12 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
python -m pip install -e .
python -m Mailroom create-config   # no flags = Ollama standard profile
python -m Mailroom doctor          # checks Python, config, database, and the Ollama endpoint
python -m Mailroom init-db
```

Bring in local mail — either the bundled synthetic fixtures or your own `.eml`
export (a single file or a folder, recursive by default):

```bash
python -m Mailroom ingest Mailroom/fixtures
# ...or your own export:
python -m Mailroom ingest ~/mail-export
```

Suggest labels locally, then review and correct in the browser:

```bash
python -m Mailroom classify --limit 10
python -m Mailroom review
```

Open http://127.0.0.1:5000 to accept, correct, or skip suggestions. Every choice
is stored in the local SQLite database; the review page never changes Gmail.
State-changing requests are same-origin and loopback-only. Export whenever you
like, from the UI or headlessly:

```bash
python -m Mailroom export --format csv --output choices.csv
python -m Mailroom export --format json --output choices.json --include-skipped
```

Nothing on this path contacts Gmail or asks for credentials.

### Hardware profiles

`create-config --profile <name>` selects the local backend written to
`config.json`. Existing configs are never rewritten; edit `config.json` or
recreate the config to change profiles.

| Profile | Flag | Backend |
|---|---|---|
| standard (default) | `--profile standard` | Ollama `qwen2.5:7b` at `:11434` |
| lightweight | `--profile lightweight` | Ollama `qwen2.5:3b` at `:11434` |
| tabby | `--profile tabby` | TabbyAPI EXL3 model at `:8080` |

Ollama profiles need Ollama installed and running; see the
[Ollama Quickstart](https://docs.ollama.com/quickstart).

## Optional: Gmail synchronization

To mirror reviewed labels into Gmail (or pull label edits made in Gmail back
into your decisions), install the optional extra and authenticate:

```bash
pip install -e '.[gmail]'
python -m Mailroom auth      # read + label changes only
python -m Mailroom scan      # fetch a bounded Gmail sample into the same database
```

Place a Google Cloud **Desktop** OAuth client JSON as `credentials.json` in the
data directory (or next to a custom `--config` file):

- Windows: `%LOCALAPPDATA%\Mailroom\credentials.json`
- Linux/macOS: `~/.config/mailroom/credentials.json` (or `$XDG_CONFIG_HOME/mailroom/`)

`auth` requests `gmail.modify` (read plus label changes on messages). It never
grants archive/delete/trash/spam/send, and the app only calls `labels.create`
and `messages.modify`. Personal @gmail.com accounts usually work after consent;
Google Workspace / custom-domain accounts may need the client added as a trusted
internal app or admin approval of the scope.

If you authenticated before label support existed (a `gmail.readonly` token),
run `python -m Mailroom auth --reauth` once before using `apply-labels`.

Pushing decisions and pulling edits back is covered in **Applying labels in
Gmail (optional)** below.

## Configuration

Copy `config.example.json` or edit the config in the data directory
(`%LOCALAPPDATA%\Mailroom\config.json` on Windows,
`~/.config/mailroom/config.json` on Linux):

```json
{
  "model": {
    "provider": "ollama",
    "endpoint": "http://127.0.0.1:11434",
    "id": "qwen2.5:7b"
  },
  "timeout": 30.0,
  "sample_limit": 100,
  "labels": [
    {
      "id": "Type/Receipt",
      "name": "Receipt",
      "axis": "kind",
      "description": "Invoice, payment, or order confirmation.",
      "examples": ["Order confirmation", "Payment receipt"],
      "exclusions": ["Shipping update", "Newsletter"]
    },
    {
      "id": "Retention/Forever",
      "name": "Forever",
      "axis": "retention",
      "description": "Keep indefinitely; invoices and receipts always go here.",
      "examples": ["Tax receipt", "Signed contract"],
      "exclusions": ["OTP", "Newsletter"]
    }
  ],
  "debug": false
}
```

## Commands

| Command | Description |
|---|---|
| `auth` | *Optional Gmail sync* — authenticate with Gmail (`gmail.modify`: read + label changes; stores refresh token in the OS credential store). `--reauth` forces a new browser login. |
| `scan` | *Optional Gmail sync* — fetch a bounded inbox sample (default 100, cap 1000) as they arrive. `--classify` or `--suggest-labels` (not both) run local inference while download continues |
| `ingest` | Ingest local `.eml` files or directories into SQLite offline (`<path>`, `--account`, `--limit`, `--no-recursive`) — the primary, Gmail-free path |
| `classify` | Classify locally. Cached mail: `--message-id` (one) or `--limit N` (messages with no proposal yet; max 1000), plus `--reclassify`/`--offset`. Direct, no database: `--text`, `--file`, or `--stdin`; prints one JSON object |
| `suggest-labels` | Propose a personal label list from cached mail (local model; timestamped JSON; does not write Gmail) |
| `pilot-report` | Step 5 metrics from your reviews (local model commentary; flags similar subjects with different labels) |
| `sync-review` | Export reviews; if conflicts remain, stop for the WebUI picker; otherwise refresh config.json examples |
| `apply-labels` | *Optional Gmail sync* — create/apply your reviewed labels in Gmail. Dry run by default (writes `apply-labels-plan.csv`); pass `--apply` to write |
| `sync-labels` | *Optional Gmail sync* — read Gmail labels back for applied mail and record your edits as decisions (read-only against Gmail) |
| `review` | Start the local review server (alias: `run-server`) |
| `export` | Export reviewed choices as CSV or JSON (`--format`, `--output`, `--include-skipped`) |
| `init-db` | Initialize the database |
| `fix-schema` | Verify/update database schema |
| `reset-db` | Reset database (delete all data) |
| `clear-cache` | Clear cached message text and proposals (keeps decisions) |
| `create-config` | Create initial configuration file (no flags = Ollama `standard`; `--profile tabby` uses TabbyAPI) |
| `doctor` | Check environment health: Python, config, database schema, loopback safety, and local backend availability (Ollama/TabbyAPI). `--strict` makes warnings fail |

## Standalone classification (no Gmail, no database)

Classification can be called directly on text, a local `.eml` file, or piped
input. Direct mode never opens the database, never touches Gmail, and prints a
single JSON object to stdout:

```bash
python -m Mailroom classify --text "Your receipt for order #1: $19.99"
python -m Mailroom classify --file invoice.eml
printf '%s' "$BODY" | python -m Mailroom classify --stdin
```

The JSON contains `label_ids`, `label_names`, `reason`, `abstain`, and
`confidence` — never the input text. `--stdin` reads raw body text; use
`--file` for `.eml` parsing (inert, HTML stripped). Failures print
`{"error": "..."}` to stderr and exit non-zero.

The same pipeline is available as a library:

```python
from Mailroom import EmailClassifier, Config, Proposal

classifier = EmailClassifier(config=Config())  # config optional; no Gmail
proposal: Proposal = classifier.classify(
    subject="Your order #123",
    sender="Shop <noreply@shop.example>",
    body="Thanks for your purchase",
)
proposal.label_ids  # ["Type/Receipt", "Purchase/Tech", "Retention/Forever"]
```

`classify(...)` also accepts a pre-built `email_text` block and an explicit
`labels=[...]` list of label definitions (defaults to the configured taxonomy).
Model endpoints must be loopback. `Config(config_path=...)` targets a specific
config file; on first use `Config()` may write `config.json` to the per-user
app directory.

REST callers can POST a direct payload — no message row needed:

```bash
TOKEN=$(curl -s http://127.0.0.1:5000/api/csrf | python -c 'import sys,json;print(json.load(sys.stdin)["csrf_token"])')
curl -s -X POST http://127.0.0.1:5000/api/classify \
  -H 'Content-Type: application/json' -H "X-CSRF-Token: $TOKEN" \
  -d '{"subject": "Your order #123", "body": "...", "sender": "Shop <noreply@shop.example>"}'
```

Direct REST requests are stateless (nothing is stored) and still require the
loopback CSRF token, like every other state-changing endpoint.

## Review and export

- The review list shows sender, subject, the current suggestion with a short
  reason, and the review status, and paginates (50 per page). The detail view
  renders cached message text as inert text (never HTML) or shows a
  cache-expired notice, plus a Gmail link only for Gmail messages.
- Suggested labels are shown separately from your saved choice. If a rerun
  produces a newer suggestion, the saved choice is kept and flagged.
- Actions: accept the suggestion, correct the labels (an explicit empty
  selection is allowed and is distinct from a skip), skip, or reopen. Accept is
  disabled for uncertain (abstained) or missing suggestions; manual correction
  still works.
- Export defaults to accepted/corrected decisions, omits bodies and reasons,
  and neutralizes spreadsheet-formula cells. Pass `--include-skipped` (CLI) or
  tick "include skipped" (web) to add explicitly skipped messages.
- Label definitions can be added, edited, and removed from the **Manage
  labels** panel. Changes are validated, written atomically to `config.json`
  (with a rolling `config.json.bak-label-edit` backup), and apply without
  restarting the server. A label referenced by a saved decision cannot be
  deleted; decisions are never rewritten or dropped.

## Applying labels in Gmail (optional)

Decisions can be pushed to Gmail and edited there instead of in the web UI.
This is the only path that writes to Gmail, and it requires an explicit
`--apply`; the review web UI never writes to Gmail.

1. `python -m Mailroom apply-labels` — dry run. Writes `apply-labels-plan.csv`
   and lists the labels it would create. Nothing is written to Gmail.
2. `python -m Mailroom apply-labels --apply` — creates any missing labels (named
   exactly like your local ids, e.g. `Type/Receipt`, `Retention/30Days`) and
   adds them to messages with an accepted/corrected decision.
3. Triage in Gmail: remove or add the labels you disagree with.
4. `python -m Mailroom sync-labels` — reads the labels back. A message you did
   not touch stays `accepted`; anything you changed is saved as a `corrected`
   decision, and the snapshot is updated so the next run is quiet.

Labels are only ever added or removed. Nothing is archived, trashed, deleted,
or marked spam, and retention labels are labels only — no mail is deleted.

## Data Storage

Data is stored in the per-user app directory (or the directory of `--config`):

- Windows: `%LOCALAPPDATA%\Mailroom\`
- Linux/macOS: `~/.config/mailroom/` (or `$XDG_CONFIG_HOME/mailroom/`)

- `Mailroom.db` — SQLite database
- `config.json` — Application configuration
- `credentials.json` — Desktop OAuth client secrets (you supply this; keep it out of source control)
- `.last_gmail_email` — non-secret pointer used to load the stored refresh token
- `suggested-labels-YYYYMMDDTHHMMSSZ.json` — each `suggest-labels` run (kept)
- `suggested-labels-latest.json` — copy of the most recent suggestion run

Refresh tokens are stored in the OS credential store (DPAPI on Windows, Secret
Service/keyring on Linux), not in the database.

`clear-cache` removes cached bodies and proposals only. Subjects, Gmail IDs, review decisions, reasons, and exports can also be sensitive and are not removed by `clear-cache`.

Gmail-cached body previews older than seven days are cleared when the app opens the database (`scan`, `init-db`, `review` if the DB already exists). Body text ingested from `.eml` files is kept. IDs and review records are kept.

## Development

```bash
pip install -e '.[dev]'
python -m pytest
```

### Browser verification tests

The inertness and link checks run against a real headless Chromium. They are an
opt-in extra, so the default `pytest` run does not start a browser:

```bash
pip install -e '.[browser]'
python -m playwright install chromium
python -m pytest -m browser -v
```

Set `MAILROOM_REQUIRE_BROWSER=1` to make a missing Playwright or Chromium a hard
failure instead of a skip. CI uses it so the browser job cannot pass without
executing the suite:

```bash
MAILROOM_REQUIRE_BROWSER=1 python -m pytest -m browser -v
```

