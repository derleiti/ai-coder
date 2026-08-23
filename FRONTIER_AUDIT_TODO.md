# AICoder v1.2 - Frontier Audit TODO

Source: GPT-5.6 Sol read-only audit against baseline `67bae9a`.
Working repository: `/home/zombie/ai-coder`.

Status legend: `TODO` -> not yet reproduced on current HEAD; `CONFIRMED` -> reproduced; `FIXED` -> patched and regression-tested; `ACCEPTED` -> intentional behavior/no code change; `STALE` -> finding no longer applies to current HEAD.

## P0/P1 - correctness and reliability

- [ ] **F01 HIGH - GUI Stop does not cancel the underlying HTTP/provider request** (`TODO`)
  - Reproduce on current HEAD.
  - Define cancellable transport contract without reintroducing hard model-turn deadlines.
  - Ensure Stop cannot leave an orphan request that races a later Retry/Resume.
  - Add regression test observing transport cancellation.

- [ ] **F02 HIGH - Successful fallback is not promoted to the effective model for later turns** (`TODO`)
  - Reproduce on current HEAD.
  - Decide explicit policy: promote fallback for the remainder of the run unless configured otherwise.
  - Keep tool-protocol/capability mode consistent with the effective model.
  - Add multi-turn regression test.

- [ ] **F03 HIGH - Completion audit can be bypassed when final tool turn also contains `DONE:`** (`TODO`)
  - Reproduce on current HEAD.
  - Route every final completion through one shared finalization gate.
  - Add structured-task regression: mutation + verification + final tool call + `DONE:` must still receive exactly one completion-audit turn.

- [x] **F04 MEDIUM - Read-only shell/binary_exec incorrectly counted as mutation** (`FIXED`)
  - Fixed in `14df94b` with separate mutation-effect classification.
  - Regression coverage added for read-only and mutating command runners.

## P1 - model state, context, transport

- [ ] **F05 MEDIUM - Chat and Settings model selection can temporarily diverge** (`TODO`)
  - Verify precedence and intended override semantics on current HEAD.
  - Add explicit run-start observability: configured/chat/effective/fallback/provider/tool mode.
  - Add regression test for deliberate chat override.

- [ ] **F06 MEDIUM - Async model-list refresh can overwrite a manual Chat selection** (`TODO`)
  - Reproduce with delayed model loader.
  - Add generation/dirty guard so user edits after load start win.
  - Add GUI regression test.

- [ ] **F07 MEDIUM - Reselecting same model may resync GUI but does not prove provider-route reset** (`TODO`)
  - Treat as observability/state-consistency item, not assumed provider bug.
  - Verify same-ID save behavior and cache invalidation behavior.
  - Close as `ACCEPTED` if no hidden local state remains after F05/F06/F08.

- [ ] **F08 MEDIUM - Model capability cache is global rather than endpoint/account scoped** (`TODO`)
  - Reproduce with two clients exposing different metadata.
  - Partition cache by endpoint + anonymized account/token identifier.
  - Add isolation regression test.

- [ ] **F09 MEDIUM - Context trimming uses message count only, not model/token budget** (`TODO`)
  - Preserve native assistant/tool adjacency.
  - Add bounded token/character budget using effective model context metadata.
  - Add large text/native tool-history regressions.

- [ ] **F10 MEDIUM - Keepalive telemetry is not separated from real model output** (`TODO`)
  - Preserve keepalive as inactivity activity.
  - Record keepalive count/timestamps separately from provider/model data.
  - Add stream regression with multiple keepalives then final JSON.

- [ ] **F11 MEDIUM - Direct OpenAI-compatible transport has different timeout/keepalive semantics** (`TODO`)
  - Verify current preview path.
  - Document semantic difference and add activity-aware streaming/telemetry if appropriate.
  - Add long-turn transport regression.

## P2/P3 - documentation, persistence, edge cases

- [ ] **F12 MEDIUM - Architecture docs still describe removed continuation timeout policy** (`TODO`)
  - Update docs to the current inactivity-only semantics.
  - Ensure GUI/help/docs describe one meaning for `request_timeout`.

- [ ] **F13 LOW - Resume intentionally does not persist raw tool evidence** (`TODO`)
  - Verify privacy/security behavior and fresh-inspection gate.
  - Expected likely resolution: `ACCEPTED`, with clearer UX/status wording.

- [ ] **F14 LOW - Evidence fast-path can miss same-size content with restored mtime** (`TODO`)
  - Reproduce edge case.
  - Add optional forced hash/recheck for explicit or security-sensitive verification.
  - Add regression test.

- [ ] **F15 LOW - Duplicate guard can block legitimate repeated read-only polling** (`TODO`)
  - Reproduce legitimate polling case.
  - Keep repeated mutations blocked.
  - Permit justified read-only polling only with bounded/state-aware policy.

## Verified mechanisms / findings that should normally be preserved

- [ ] **F16 INFO - Tool-result handoff works in the normal live loop** (`TODO verification on current HEAD`)
  - Preserve text `current_input` handoff and native `assistant.tool_calls -> role=tool` history.
  - Extend regressions for multi-result, transient failure, trim boundary and resume.

- [ ] **F17 INFO - TriForce host boundary is strong but name filtering alone is not a mathematical guarantee** (`TODO verification on current HEAD`)
  - Preserve catalogue filtering + dispatch-time blocking + local/remote separation.
  - Review semantic target metadata/RBAC hardening without weakening local operator power.

## Cross-cutting test/UX work from the audit

- [ ] **X01** Full regression suite after every coherent fix; document environment-only skips/errors separately.
- [ ] **X02** Provider/transport/local-handoff timing instrumentation with request IDs.
- [ ] **X03** Status UI must distinguish `tool completed` from subsequent `waiting for model`.
- [ ] **X04** Resume UI should state that raw evidence is not restored and fresh inspection is required.
- [ ] **X05** Keep provider-specific quirks in capability/transport layers; no scattered model-name hacks.

## Do not regress

- No hard total model-turn deadline while backend/network keepalive activity continues.
- No automatic retry of mutating MCP calls without an idempotency contract.
- No raw secret-bearing tool output in persistent journals.
- No weakening of the TriForce-host administration boundary.
- No weakening of legitimate local shell/DevOps/system capabilities.
- Security risk classification stays separate from progress/verification semantics.
- Text tool protocol remains first-class for smaller models.
