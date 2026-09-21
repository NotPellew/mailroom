# Step 4: Review and export

Read ../AGENTS.md and shared.md, then implement only this step. Inspect the existing code and docs/progress.md if present; do not load other step documents unless a specific dependency requires it. PLAN.md is optional background.

**Inputs:** cached messages, proposals, label definitions, and local decisions.

**Build:**
- Review list showing sender, subject, suggestion, short reason, and status. Detail view shows inert text or a cache-expired notice plus a Gmail link.
- Accept suggestions, correct the selected labels (including an explicit empty selection), skip, and reopen/edit decisions. Do not allow Accept on failed/abstained proposals; manual correction remains available.
- Filter by proposed label, review status, and uncertain/error state. Show final choices separately from any later proposal. Use visible terminology such as “Save choice locally.”
- Save changes transactionally. Validate chosen labels on the server. Protect state-changing local requests against cross-site submissions; bind to loopback and reject unexpected hosts/origins.
- Export CSV and JSON with account ID, message ID, thread ID, final label IDs/names, status, and review timestamp. Default to accepted/corrected decisions; allow explicit inclusion of skipped records. Do not export bodies or reasons by default. Neutralize spreadsheet formula cells in CSV exports.

**Acceptance:** accept/correct/skip/reopen work across restart and reclassification; empty selections remain distinguishable from skips. Exports match the saved selections. HTML/script fixtures remain inert with no remote loads. Verify Gmail links against the connected account and a real sampled message. No control changes Gmail.

## Handoff

Record completed work, verification commands/results, and unresolved blockers in docs/progress.md for the next implementer. Keep that handoff concise. Distinguish fixture checks from live checks; do not claim unavailable live checks passed.

