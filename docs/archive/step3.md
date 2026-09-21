# Step 3: Local classification

Read ../AGENTS.md and shared.md, then implement only this step. Inspect the existing code and docs/progress.md if present; do not load other step documents unless a specific dependency requires it. PLAN.md is optional background.

**Inputs:** stored messages, editable label vocabulary, verified TabbyAPI endpoint/model.

**Build:**
- Probe the existing local API using its actual documentation/configuration without printing keys. Test the installed Qwen2.5-7B baseline if compatible and available; avoid disrupting an already loaded model. Record the model actually used.
- Define initial categories Type/VerificationCode, Type/Newsletter, Type/Receipt, Type/SecurityAlert, and Action/NeedsReply, with inclusion/exclusion examples. Reuse suitable existing labels; distinguish login codes from recovery codes and security alerts. Support optional exact template rules; do not invent trusted sender rules without reviewed examples.
- Use one bounded message per request initially. Delimit email text as data and instruct the model to abstain when ambiguous. No tool execution.
- Require a structured result: `label_ids` (unique allowed IDs), `reason` (short plain text), and `abstain` (boolean). Abstention requires an empty label list. Validate locally even if server-side structured output is supported.
- Unknown IDs, invalid output, timeouts, and model outages become visible errors/review states, never successful guesses. Use bounded retries for transient failures; do not retry indefinitely or fall back to cloud.
- Store proposals separately from final choices. Reclassification is explicit and retains prior proposals and all user decisions. Include input/version fingerprints and timing for reproducibility.

**Acceptance:** fixture tests cover valid multi-label results, no-label results, abstention, unknown IDs, malformed JSON, embedded malicious instructions, and unavailable inference. One live local classification succeeds when runtime is available. Injection examples are also inspected in the pilot; prompt wording alone is not evidence of immunity.

**Excluded:** model fine-tuning, embeddings, autonomous agents, and unmeasured model-confidence thresholds.

## Handoff

Record completed work, verification commands/results, and unresolved blockers in docs/progress.md for the next implementer. Keep that handoff concise. Distinguish fixture checks from live checks; do not claim unavailable live checks passed.

