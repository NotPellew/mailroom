# Shared implementation decisions


- Use Python, Flask with server-rendered templates, standard-library SQLite, and a small CLI. Avoid a separate frontend build. Use a Python virtual environment and commit reproducible dependency versions.
- Keep modules for Gmail reads, classification, storage, and review separate. Use Google-supported OAuth/API libraries and a simple HTTP client for TabbyAPI.
- Configuration: loopback model endpoint, model ID, timeouts, sample limit (default 100), and editable label definitions/rules. Validate configuration at startup. Never infer the endpoint or model ID from a directory name.
- Store app data under `%LOCALAPPDATA%\EmailMan`; protect credentials with Windows credential storage/DPAPI. Keep credentials and runtime data out of source control. Do not modify existing AI installations or switch a running model without establishing it is appropriate.
- Support one Gmail account in V1, but key message records by account plus Gmail message ID. Store thread IDs separately. Refuse accidental account mixing.
- Keep messages, versioned proposals, and human decisions separate. Review states: pending, accepted, corrected, skipped. A skipped message has no final selection; an accepted/corrected selection may explicitly contain zero labels.
- Labels have stable local IDs, display names, definitions, and exclusions. Existing Gmail labels are reference data; do not automatically use every existing label as an AI category. Record matching Gmail label IDs where relevant, without creating labels.
- Use a manual workflow: `auth`, `scan`, `classify`, `review`, `export`, and `clear-cache`. No background scheduler. A failed step must be resumable.


