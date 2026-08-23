# AICoder v1.2 - Frontier Audit TODO

Source: GPT-5.6 Sol read-only audit against baseline `67bae9a`.
Working repository: `/home/zombie/ai-coder`.

Status legend: `TODO` -> not yet reproduced on current HEAD; `CONFIRMED` -> reproduced; `FIXED` -> patched and regression-tested; `ACCEPTED` -> intentional behavior/no code change; `STALE` -> finding no longer applies to current HEAD.

## P0/P1 - correctness and reliability

- [x] **F01 HIGH - GUI Stop does not cancel the underlying HTTP/provider request** (`FIXED`)
  - Reproduced: Stop exits the runtime while the blocking provider thread can continue until transport return.
  - Added optional transport `cancel_current_request()` contract without reintroducing hard model-turn deadlines.
  - TriForce and direct OpenAI-compatible transports close the active HTTP response handle best-effort on Stop.
  - Regression coverage verifies runtime cancellation and active-response closure.

- [x] **F02 HIGH - Successful fallback is not promoted to the effective model for later turns** (`FIXED`)
  - Reproduce on current HEAD.
  - Decide explicit policy: promote fallback for the remainder of the run unless configured otherwise.
  - Keep tool-protocol/capability mode consistent with the effective model.
  - Fixed: successful provider fallback is promoted for the remainder of the run and capability/tool-protocol selection follows it.
  - Multi-turn regression added.

- [x] **F03 HIGH - Completion audit can be bypassed when final tool turn also contains `DONE:`** (`FIXED`)
  - Reproduce on current HEAD.
  - Route every final completion through one shared finalization gate.
  - Fixed: final tool-call `DONE:` path now passes through the same one-shot completion-audit requirement.
  - Structured-task regression added: mutation + verification + final tool call + `DONE:` receives exactly one audit turn.

- [x] **F04 MEDIUM - Read-only shell/binary_exec incorrectly counted as mutation** (`FIXED`)
  - Fixed in `14df94b` with separate mutation-effect classification.
  - Regression coverage added for read-only and mutating command runners.

## P1 - model state, context, transport

- [x] **F05 MEDIUM - Chat and Settings model selection can temporarily diverge** (`FIXED`)
  - Verify precedence and intended override semantics on current HEAD.
  - Added run-start route observability: configured/effective/fallback/provider/transport/tool protocol/context budget.
  - Deliberate Chat override is explicit per-run behavior and covered by GUI model-selection regressions.

- [x] **F06 MEDIUM - Async model-list refresh can overwrite a manual Chat selection** (`FIXED`)
  - Reproduced in the Chat-tab refresh path. Added dirty guards so manual Chat edits after load start win.
  - GUI regressions cover manual override, settings sync, and dirty reset.

- [x] **F07 MEDIUM - Reselecting same model may resync GUI but does not prove provider-route reset** (`ACCEPTED`)
  - Treat as observability/state-consistency item, not assumed provider bug.
  - Verify same-ID save behavior and cache invalidation behavior.
  - Accepted after F05/F06/F08: no additional same-ID provider-route state was found locally; endpoint/account capability cache is now scoped.

- [x] **F08 MEDIUM - Model capability cache is global rather than endpoint/account scoped** (`FIXED`)
  - Reproduce with two clients exposing different metadata.
  - Cache partitioned by endpoint + anonymized token/account identifier, with client-instance fallback.
  - Isolation regression added.

- [x] **F09 MEDIUM - Context trimming uses message count only, not model/token budget** (`FIXED`)
  - Preserve native assistant/tool adjacency.
  - Added model-context-derived character budget with conservative fallback.
  - Large-text and native assistant/tool adjacency regressions added.

- [x] **F10 MEDIUM - Keepalive telemetry is not separated from real model output** (`FIXED`)
  - Preserve keepalive as inactivity activity.
  - Keepalive chunks/timestamps and payload chunks are recorded separately.
  - Stream telemetry regression added.

- [x] **F11 MEDIUM - Direct OpenAI-compatible transport has different timeout/keepalive semantics** (`FIXED`)
  - Verify current preview path.
  - Direct preview transport now explicitly reports non-streaming blocking-request timeout semantics in telemetry.
  - Transport regression added; true streaming/cancellation remains future transport work.

## P2/P3 - documentation, persistence, edge cases

- [x] **F12 MEDIUM - Architecture/docs timeout wording stale after inactivity-only change** (`FIXED`)
  - Update docs to the current inactivity-only semantics.
  - Settings/help wording now defines `request_timeout` as provider/network inactivity, reset by streaming keepalive activity, not a hard turn deadline.

- [x] **F13 LOW - Resume intentionally does not persist raw tool evidence** (`ACCEPTED`)
  - Verify privacy/security behavior and fresh-inspection gate.
  - Accepted privacy behavior: journals persist sanitized context/tool metadata only; resume mutation is gated on fresh inspection.
  - GUI now states this explicitly on resumed runs.

- [x] **F14 LOW - Evidence fast-path can miss same-size content with restored mtime** (`FIXED`)
  - Reproduce edge case.
  - Repeated-read evidence recall now forces a content hash before reusing cached evidence.
  - Same-size/restored-mtime regression added.

- [x] **F15 LOW - Duplicate guard can block legitimate repeated read-only polling** (`FIXED`)
  - Reproduce legitimate polling case.
  - Keep repeated mutations blocked.
  - Explicit polling/monitoring intent may repeat identical read-only calls up to a small bound; mutations remain strictly duplicate-blocked.

## Verified mechanisms / findings that should normally be preserved

- [x] **F16 INFO - Tool-result handoff works in the normal live loop** (`VERIFIED`)
  - Preserve text `current_input` handoff and native `assistant.tool_calls -> role=tool` history.
  - Verified by normal handoff, native tool-role, trim-boundary, fallback, resume, and multi-result regressions on current HEAD.

- [x] **F17 INFO - TriForce host boundary is strong but name filtering alone is not a mathematical guarantee** (`VERIFIED`)
  - Preserve catalogue filtering + dispatch-time blocking + local/remote separation.
  - Verified catalogue filtering plus dispatch-time TriForce-host blocking and local-only shell/binary/task boundaries; backend RBAC remains authoritative.

## Cross-cutting test/UX work from the audit

- [x] **X01** Final full regression suite: 435/435 tests passed; expected security-block diagnostics are not failures.
- [x] **X02** Provider/transport/local-handoff timing instrumentation uses per-turn request IDs.
- [x] **X03** Status UI distinguishes `tool completed` from subsequent `waiting for model`.
- [x] **X04** Resume UI states that raw evidence is not restored and fresh inspection is required.
- [x] **X05** Provider-specific quirks remain in capability/transport layers; no new scattered model-name hacks.

## Do not regress

- No hard total model-turn deadline while backend/network keepalive activity continues.
- No automatic retry of mutating MCP calls without an idempotency contract.
- No raw secret-bearing tool output in persistent journals.
- No weakening of the TriForce-host administration boundary.
- No weakening of legitimate local shell/DevOps/system capabilities.
- Security risk classification stays separate from progress/verification semantics.
- Text tool protocol remains first-class for smaller models.
