# Step 5: Verification, pilot, and handoff

Read ../AGENTS.md and shared.md, then implement only this step. Inspect the existing code and docs/progress.md if present; do not load other step documents unless a specific dependency requires it. PLAN.md is optional background.

**Inputs:** completed manual workflow and approximately 100 representative messages when authorized access is available.

**Build and verify:**
- Run the meaningful tests from tasks 1-4, plus interrupted-run recovery, duplicate scans, OAuth expiry, and preservation of corrections. Review the Gmail call surface and OAuth scope for read-only compliance.
- Exercise the rendered UI end to end with synthetic fixtures; run a small live scan/classify/review/export workflow when possible.
- Separate roughly 20% of the pilot as held-out examples before tuning rules/prompts. Human review supplies ground truth; do not treat the model's own suggestions as truth.
- Report per-label precision and coverage, acceptance/correction rate among reviewed proposals, abstention/error counts, and latency. State sample sizes and undefined metrics; do not claim quality from unreviewed messages or invent an accuracy target.
- Write a concise README covering install/run commands, OAuth setup, local endpoint configuration, storage/credential locations, cache clearing, troubleshooting, and the read-only boundary.
- Produce a pilot report with observed errors, limitations, and unresolved live checks. No private message bodies in tracked reports.

**Done:** a user can configure the app, fetch a bounded sample, obtain local suggestions, edit/save selections, restart without losing them, and export them. All relevant tests pass; any human review or external setup still pending is clearly identified. V1 completion never depends on Gmail writes or cleanup.

## Handoff

Record completed work, verification commands/results, and unresolved blockers in docs/progress.md for the next implementer. Keep that handoff concise. Distinguish fixture checks from live checks; do not claim unavailable live checks passed.

