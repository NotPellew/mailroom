# Step 2: Read-only Gmail integration

Read ../AGENTS.md and shared.md, then implement only this step. Inspect the existing code and docs/progress.md if present; do not load other step documents unless a specific dependency requires it. PLAN.md is optional background.

**Inputs:** task 1; desktop OAuth client setup supplied by the user when needed.

**Build:**
- Desktop OAuth using only `gmail.readonly`; protect refresh tokens and provide reauthentication instructions. Confirm personal Gmail versus Workspace restrictions during setup.
- Fetch account identity, existing label metadata, and a bounded inbox sample. Default query: `in:inbox -in:trash -in:spam -in:drafts`; cap at 100 individual messages, paginate only to the cap, and store the actual query/sample time.
- Decode MIME safely, prefer plain text, convert HTML to inert text when necessary, and skip attachments/remote resources. Store sender, subject, received timestamp, existing labels, message ID, thread ID, and capped text. Flag truncation and unsupported content.
- Upsert messages without overwriting human decisions. Do not fetch the full mailbox or all thread history. For V1, abstain on NeedsReply when the available context is insufficient.
- Expose cache clearing separately from decision deletion. Default body-cache expiry: seven days, enforced when the app runs; retain IDs and review records. Explain that subjects, reasons, and exports can also be sensitive.

**Acceptance:** synthetic MIME tests cover plain text, multipart, HTML-only, malformed input, and attachments. Repeating a scan creates no duplicate messages and preserves decisions. A live bounded scan works after OAuth setup; source inspection confirms no Gmail mutation methods or write scopes.

**If credentials are unavailable:** finish the adapter and fixtures, document the exact setup required, and leave live verification explicitly pending. Continue independent tasks.

## Handoff

Record completed work, verification commands/results, and unresolved blockers in docs/progress.md for the next implementer. Keep that handoff concise. Distinguish fixture checks from live checks; do not claim unavailable live checks passed.

