# Step 1: App skeleton and configuration

Read ../AGENTS.md and shared.md, then implement only this step. Inspect the existing code and docs/progress.md if present; do not load other step documents unless a specific dependency requires it. PLAN.md is optional background.

**Inputs:** AGENTS.md and shared.md.

**Build:**
- Package layout, dependency setup, CLI entry point, configuration example, `.gitignore`, and minimal Flask page.
- SQLite initialization/schema versioning for account metadata, cached message text, proposals, and decisions. Include timestamps and model/prompt/label-definition versions on proposals.
- Validate label IDs, duplicate names, limits, and loopback-only model/UI addresses. Keep secrets out of configuration examples and logs.
- Add synthetic mail fixtures for development; never commit real email content.

**Acceptance:** a fresh environment can initialize the database and open the review shell without Gmail credentials or a running model. Restart preserves stored fixture records. Invalid configuration produces an actionable error.

**Excluded:** real Gmail access and classification logic.

## Handoff

Record completed work, verification commands/results, and unresolved blockers in docs/progress.md for the next implementer. Keep that handoff concise. Distinguish fixture checks from live checks; do not claim unavailable live checks passed.

