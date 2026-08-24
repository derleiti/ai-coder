# AICoder Experimental — RAM Team Runtime

Status: experimental design target
Branch: `aicoder-experimental`

## Goal

Evolve AICoder from a single shared-workspace coding agent into a latency-aware,
transactional multi-agent development runtime. The default execution path should
prefer RAM-backed isolated workspaces when safe and automatically fall back to
the normal filesystem when memory or platform constraints make RAM unsuitable.

The real user workspace must remain unchanged while candidate agents inspect,
edit, build and test their variants. Only a verified final result is persisted.

## Core invariants

1. The original workspace is the authoritative persistent source and remains
   untouched during candidate execution.
2. Every candidate starts from the same immutable base revision/state.
3. Candidate writes are isolated from all other candidates.
4. RAM use is bounded by an explicit safe memory budget and never forces the
   host into destructive memory pressure or swap thrashing.
5. Disk execution remains a transparent fallback and preserves functionality.
6. Recovery metadata and compact patches/checkpoints survive process failure.
7. No candidate is merged based only on model opinion. Tests and measurable
   evidence are evaluated first.
8. A merged tree is treated as a new candidate and must pass fresh verification.
9. The final write to the real workspace is transactional/atomic where possible.
10. Performance telemetry is collected by default without requiring a user
    setting; only meaningful bottlenecks are surfaced prominently.

## Default runtime shape

User task
  -> Task Contract
  -> Research Team (up to 4 agents)
  -> Implementation Planner (1 agent)
  -> Candidate Coders (up to 4 isolated variants)
  -> Deterministic Evaluation
  -> Merge / Integration Agent
  -> Finalizer
  -> Full verification
  -> Atomic persistence

The scheduler may use fewer agents for simple tasks. Nine agents are a maximum
quality profile, not a mandatory cost for every request.

## Research roles

- Primary Sources: current official documentation, APIs, release notes, upstream.
- Best Practices: current architectural and implementation practice.
- Security/Reliability: risks, failure modes, recovery, privilege boundaries.
- Alternatives: deliberately different or unconventional viable approaches.

Research output is normalized into evidence for the planner instead of being
passed as uncontrolled prose directly to coders.

## Planning

One planner produces a shared Task Contract containing:

- objective
- explicit requirements
- constraints
- architecture boundaries
- acceptance tests
- security expectations
- compatibility requirements
- verification commands

All candidate coders receive the same contract and same base workspace.

## Candidate strategies

Candidates should be intentionally diverse, for example:

- conservative / minimal change
- architecture-first
- performance-oriented
- robustness / security-first

Each candidate runs its own NativeLightRuntime against an isolated workspace.

## RAM workspace architecture

Preferred Linux implementation:

- immutable base workspace
- tmpfs-backed candidate upper/work directories
- copy-on-write or equivalent overlay view per candidate
- optional in-process ProjectCache for file contents, hashes, symbols and indexes
- build/test temporary data stays in RAM when practical
- compact persistent recovery journal outside volatile RAM

Do not duplicate a complete repository for every candidate when a shared base
plus copy-on-write layers can provide isolation.

## Automatic memory policy

The runtime measures:

- available physical memory
- current process/system pressure
- repository working-set estimate
- expected number of variants
- candidate upper-layer growth

Execution mode:

- RAM when safe
- reduced candidate count when RAM is constrained
- filesystem fallback when necessary

The user should not lose capability merely because RAM acceleration is unavailable.

## Evaluation

Deterministic evidence precedes LLM judgment. Candidate reports should include
where applicable:

- unit/integration test results
- regression tests
- lint/format checks
- type checks
- security checks
- requirement coverage
- runtime/build performance
- changed-file and complexity metrics
- failures and retries

A candidate with persuasive prose does not outrank a candidate with better
verified behavior.

## Merge model

The system may choose a complete winning candidate or combine compatible parts
from several candidates. A component-level merge must account for dependencies;
"best file" does not automatically mean "best integrated system".

After merge:

1. create a new integrated candidate
2. run full verification from a clean state
3. repair integration failures if justified
4. repeat verification
5. only then persist the resulting diff

## Persistence and recovery

Volatile work can be discarded freely, but recovery state is persisted in small
artifacts such as:

- task-contract.json
- plan.json
- candidate metadata
- candidate patches/diffs
- evaluation results
- merge state

The complete RAM tree does not need to be continuously written to disk.

## Performance telemetry

Always collect low-overhead timing data for:

- provider/API round trip
- model response latency
- tool execution
- filesystem operations
- subprocess/test/build time
- orchestration overhead
- total wall time vs accumulated parallel agent time

Surface warnings only when a bottleneck is meaningful, e.g. high model latency,
network delay, slow filesystem I/O, memory pressure or a slow test/build phase.

The UI should help the user distinguish "buy a faster disk" from "choose a
lower-latency model" instead of guessing.

## UI philosophy

Avoid a settings explosion.

Suggested persistent workspace setting:

Execution workspace:
- Auto (default)
- RAM
- Disk

Performance data is measured automatically. Detailed telemetry can be expanded
in the run UI; significant problems are shown proactively.

Multi-agent composition belongs primarily to task/run configuration or presets,
not dozens of permanent global settings.

Possible presets:

- Fast
- Balanced
- Maximum Quality
- Free Models
- Custom

## Implementation phases

### Phase 1 — Observability
- unified timing/event metrics
- bottleneck attribution
- CLI and GUI performance summary
- regression tests for telemetry overhead and accuracy boundaries

### Phase 2 — Transactional Workspace
- WorkspaceBackend abstraction
- DiskWorkspace backend preserving current behavior
- RAMWorkspace backend with safe budget/fallback
- dirty-file tracking and final diff
- crash-safe recovery journal
- atomic verified persistence

### Phase 3 — Isolated Candidates
- shared immutable base
- independent candidate workspaces
- candidate lifecycle and cleanup
- per-candidate plans, logs and metrics
- tests proving isolation

### Phase 4 — Research + Planner Pipeline
- structured research roles
- evidence normalization
- shared Task Contract
- planner verification requirements

### Phase 5 — Parallel Candidate Runtime
- multiple selected coding models
- bounded parallel execution
- cancellation of dominated/broken candidates
- provider concurrency/rate-limit awareness

### Phase 6 — Evaluation and Merge
- deterministic scoring/evidence
- merge planner
- integrated candidate
- clean full verification
- transactional final commit

### Phase 7 — Adaptive Team Runtime
- automatically choose team size by complexity/risk/resources
- historical role/model performance signals
- cheap/fast models for suitable merge/finalization tasks
- optional iterative candidate evolution only after the simpler system is proven

## Explicit non-goals for the first implementation

- no recursive unlimited agent spawning
- no automatic GitHub PR spam
- no blind merging based on LLM preference
- no mandatory RAM requirement
- no replacing the existing NativeLightRuntime before the new path proves itself
- no large persistent workspace copies merely to simulate branches

## Success criterion

The experimental runtime is ready to replace the old roadmap only when it can
prove end-to-end that it can:

1. create an isolated RAM-backed candidate from a real repository
2. mutate and test it without modifying the source workspace
3. run at least two independent candidate implementations
4. evaluate them using deterministic evidence
5. integrate a selected/merged result
6. verify the integrated result from a clean state
7. atomically persist the final changes
8. recover safely from an interrupted run
9. transparently fall back to disk mode under constrained RAM
10. report where execution time was actually spent
