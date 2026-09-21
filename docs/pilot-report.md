# EmailMan V1 pilot report

Computed at 2026-09-14T20:43:12.386465+00:00.
Local model id: `qwen3.8-27b-gsq-rco-iq3_s`.
Ground truth: **human review decisions only**. Unreviewed mail is not used.
No message bodies or subjects are included.
Proposals scored: **latest per message** (reclassification; human labels frozen).

## Sample

- Reviewed (accepted/corrected/skipped): **118**
- Held-out slice: **20%** of reviewed (24 messages), last by sorted message id
- Train / calibration slice: 94 messages
- Inference latency: Per-message inference latency was not recorded.

## Agreement with the reviewer (all reviewed)

- Accepted as-is: 40
- Corrected: 78
- Skipped: 0
- Accept rate among scored: 0.34
- Model abstain among scored: 0

## Per-label precision / recall (all reviewed, scored only)

| Label | Human | Predicted | Precision | Recall |
|---|---:|---:|---:|---:|
| `Type/Newsletter` | 35 | 30 | 0.97 | 0.83 |
| `Type/Targeted` | 32 | 42 | 0.71 | 0.94 |
| `Type/Personal` | 0 | 0 | undefined (no cases) | undefined (no cases) |
| `Type/NeedsReply` | 0 | 0 | undefined (no cases) | undefined (no cases) |
| `Type/NeedsAction` | 9 | 15 | 0.33 | 0.56 |
| `Type/SecurityAlert` | 12 | 11 | 1.00 | 0.92 |
| `Type/Verification` | 5 | 4 | 1.00 | 0.80 |
| `Type/Receipt` | 27 | 24 | 0.96 | 0.85 |
| `Type/ShippingUpdate` | 8 | 7 | 1.00 | 0.88 |
| `Purchase/Tech` | 15 | 10 | 0.60 | 0.40 |
| `Purchase/FoodDrink` | 11 | 10 | 1.00 | 0.91 |
| `Retention/30Days` | 65 | 52 | 0.94 | 0.75 |
| `Retention/1Year` | 21 | 39 | 0.46 | 0.86 |
| `Retention/Forever` | 32 | 27 | 0.93 | 0.78 |

## Held-out (not for tuning)

- n=24, accepted=14, corrected=10
- Accept rate: 0.58

## Label conflicts (similar phrases, different human labels)

- Conflict groups: **1**
- Messages in those groups: **4**

Phrases are omitted from the tracked report; see the local conflicts JSON.

## Limitations

- Gmail labels are **not** applied. V1 is read-only.
- Live Gmail link clicks and browser inertness were not automated here.
- Precision is undefined when the model never predicted a label.
- Agreement is capped by the saved labels themselves; where the reviewer's decisions are internally inconsistent, no model can score well against them.
- Do not treat these rates as a product accuracy target.
