# EmailMan V1 project plan

See `IMPLEMENTATION.md` for the sequential implementation tasks, concrete defaults, and acceptance criteria.

## Outcome and decision

Help choose useful labels for Gmail messages using existing local models. V1 fetches messages, proposes labels, and lets the user accept, correct, or skip suggestions locally.

Proceed. Confidence is high in feasibility and medium in classification accuracy until tested on actual mail. The weakest link is whether label definitions reflect the user's needs. Start with a small reviewed sample. Exact rules are the simpler alternative for predictable templates.

## V1 scope

- Gmail access limited to reading and label management: create labels and add/remove labels on messages. No archive, delete, trash, spam, or send.
- Configurable label vocabulary with clear definitions and examples.
- Local model suggestions with short reasons and an uncertain/skip outcome.
- A local review page for choosing labels and recording corrections.
- Saved decisions and an exportable review report.

Interpretation: choices are stored locally and can be pushed to Gmail as labels by an explicit, manual command (`apply-labels`), edited in Gmail, and pulled back (`sync-labels`). Applying labels is opt-in and never automatic. Cleanup, archiving, deletion, retention policies, cleanup previews, and scheduled automation remain outside scope. Retention is represented as a label only; nothing is deleted or archived and no cleanup code is included.

## Architecture

- Python app, Gmail API with desktop OAuth (gmail.modify: read plus label changes; no delete/archive), existing TabbyAPI on loopback, and SQLite for proposals and reviewed choices.
- Small local review page bound to loopback. Gmail remains the email interface.
- Pipeline: fetch bounded sample -> normalize text -> match explicit rules -> classify unresolved messages locally -> validate result -> review label choices -> save/export -> optionally apply labels to Gmail and sync edits back.
- Model output is limited to configured labels, reasons, and an abstain outcome. Multiple labels are allowed where definitions support them.
- Treat email text as untrusted data. Render inert escaped text; do not load remote images, follow links automatically, execute attachments, or obey embedded instructions.
- Protect OAuth credentials using Windows credential protection and keep them outside the repository. Avoid logging bodies or verification codes; use bounded, user-clearable review caches.
- No cloud inference fallback, additional runtime, hosted service, or vector database.

## Initial label vocabulary

Proposed starting labels, to refine during the pilot:

- Type/VerificationCode
- Type/Newsletter
- Type/Receipt
- Type/SecurityAlert
- Action/NeedsReply

Inspect existing Gmail labels before finalizing names. Reuse useful categories rather than introduce duplicates. Define inclusion/exclusion examples, especially verification codes versus recovery codes and security alerts. An uncertain result stays in the review queue rather than forcing a label.

## Phase 1: Verify runtime and Gmail reads

1. Inspect TabbyAPI documentation and endpoint without exposing credentials. Confirm local inference and validate structured output in the app.
2. Confirm runtime compatibility and available GPU memory. Start with installed Qwen2.5-7B-Instruct EXL3 if compatible; compare a larger installed general-purpose model only if needed.
3. Establish personal Gmail versus managed Workspace and configure desktop OAuth with read-only access.
4. Read existing labels and fetch approximately 100 recent inbox messages spanning relevant categories and languages. Exclude attachments from V1 processing.

Exit: local inference and authorized Gmail reads work; pilot messages are available. Evaluate existing models before downloading another.

## Phase 2: Define labels and generate suggestions

1. Write editable definitions, examples, and exclusions for each label.
2. Extract subject, headers, and readable body. Flag oversized, truncated, or unsupported content for review; distinguish message IDs from thread IDs.
3. Add simple rules for well-defined recurring templates.
4. Generate validated label proposals with short reasons. Use limited thread context for NeedsReply when necessary; abstain when intent is unclear.
5. Store proposal provenance: rule or model, prompt version, and message ID. Do not treat model-reported confidence as a calibrated probability.

Exit: every message has an inspectable proposal or uncertain outcome; malformed output cannot create arbitrary categories.

## Phase 3: Review and choose labels

1. Build a local list showing sender, subject, proposed labels, reason, and Gmail link.
2. Allow accept, change labels, and skip. Save decisions locally and make them editable.
3. Filter by label, uncertain result, and review status.
4. Export reviewed choices as CSV or JSON with message IDs, labels, and review status; omit bodies by default.
5. Preserve reviewed choices across reruns; show new model proposals separately.

Exit: the user can review the pilot, correct suggestions, reopen decisions, and export choices without changing Gmail.

## Phase 4: Evaluate and package V1

1. Report acceptance/correction rates, per-label precision and coverage, abstentions, and processing time against reviewed examples.
2. Inspect errors involving security alerts, recovery codes, multilingual messages, quoted text, and ambiguous requests.
3. Improve definitions or rules before adding a larger model. Keep a held-out portion of the sample for evaluating changes.
4. Add bounded retries, resume support, and clear errors for unavailable TabbyAPI, OAuth expiry, network failures, and malformed responses.
5. Document setup, manual scan/review/export, local data storage, and cache removal.

Exit: the pilot is reviewable, decisions survive restart, reruns preserve corrections, and measured limitations are documented. Gmail writes are limited to label creation and message label changes; no archive/delete/trash/spam, and retention is a label only.

## Verification

- Label validation, abstention, conflicting categories, and malicious instructions inside messages.
- Malformed output, inference outage, Gmail read failures, duplicate scans, and persistence of corrections.
- Review controls, export correctness, inert email rendering, and Gmail links.
- Confirm Gmail writes stay limited to label creation (`labels.create`) and message label changes (`messages.modify`); no archive/delete/trash/spam, and retention is a label only.

## Deliverables

- Runnable Python app and dependency lockfile.
- Local review page, editable label definitions/rules, and SQLite decision store.
- Manual scan, review, and export workflow.
- Setup guide and pilot evaluation report.

## Remaining inputs

- Personal Gmail or Workspace and any admin restrictions.
- Working TabbyAPI endpoint and available memory.
- Existing labels, preferred categories, email languages, and daily volume.

First milestone: read approximately 100 messages and produce local label suggestions for review.
