# AICoder

[![CI](https://github.com/derleiti/ai-coder/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/derleiti/ai-coder/actions/workflows/ci.yml)
[![Security](https://github.com/derleiti/ai-coder/actions/workflows/security.yml/badge.svg?branch=master)](https://github.com/derleiti/ai-coder/actions/workflows/security.yml)

**Current release: 1.4.3** · AILinux coding/DevOps agent for TriForce, Project Memory and the Loom capability fabric.

AICoder combines a terminal agent, PyQt6 desktop UI, provider/model routing, a guarded MCP/local-tool loop, transactional coding workflows, multi-agent Team Runtime and local-first project memory. It is designed to inspect real system state, create a rollback path before risky changes, implement the smallest correct change and verify the result with executable evidence.

## Highlights

- **CLI + PyQt6 desktop UI** with system tray, model/runtime controls, workspace selection and persistent settings.
- **Canonical TriForce MCP fabric** with progressive semantic tool disclosure instead of loading a giant static catalogue into every prompt.
- **Typed local workspace capabilities** for files, code search/editing, Git, diagnostics and selected OS operations under one privilege/approval policy.
- **Provider-neutral model routing** with account-backed providers, BYOK credentials, OS-keyring storage and explicit provider availability/error handling.
- **Autonomous agent runtime** with persistent plans, resumable states, structured tool results and evidence-aware continuation.
- **Team Runtime** with research, brainstorm, planning, multiple coding candidates, merge planning, test planning and deterministic final verification.
- **Transactional candidate workspaces** with backup, conflict checks, atomic persistent writes and rollback on failed commit.
- **Project Memory** stored local-first in the existing evidence database, synchronized through TriForce and mirrored into Claude-Mem history after server acceptance.
- **Automatic Teamrun checkpoints** so long-running team work can leave structured project history without making memory authoritative over current code.
- **Feature experience / failure evidence** for reusable context and regression awareness.
- **AILinux Helper / Loom integration** for explicitly paired workspace, device and optional compute capabilities.
- **System diagnostics hardening** for frozen/GUI builds: host loader environment restoration, standard admin-path binary resolution, read-only AppArmor status checks and semantic audit-event deduplication.
- **Bounded fail-closed recovery backups** so a required fallback snapshot that exceeds its tool budget blocks the mutating operation instead of continuing without rollback protection.
- **Cross-platform packaging** for Linux, Debian/Ubuntu, Windows, Arch/AUR and Termux-oriented workflows where release artifacts are available.

## Architecture

```text
                     User
                      │
              ┌───────┴────────┐
              │                │
              ▼                ▼
          AICoder CLI      PyQt6 GUI
              │                │
              └───────┬────────┘
                      ▼
            Agent / Team Runtime
                      │
          ┌───────────┼────────────┐
          │           │            │
          ▼           ▼            ▼
   Local typed    Project      TriForce MCP
   capabilities    Memory          │
          │           │            ├─ models/agents
          │           └────sync────┤
          │                        ├─ Helper/device
          │                        └─ services/remote
          ▼
      Workspace

Accepted Project-Memory revisions
            │
            ▼
         TriForce
            │
            ▼
      Claude-Mem history
```

### Responsibility split

- **TriForce** is the remote control plane, canonical MCP schema authority, auth/RBAC layer and Project-Memory synchronization authority.
- **AICoder** owns coding workflow, local workspace policy, local evidence, Project-Memory dirty/offline state and agent/team execution.
- **AILinux Helper** exposes user-consented endpoint/workspace/device capabilities.
- **AILinux Loom** presents the wider AILinux capability/control fabric.

AICoder does not treat the TriForce host as an unrestricted coding target. Backend-admin tools are excluded from normal AICoder operation while explicitly paired Helper/workspace capabilities can remain available through their own scope.

## Evidence-first agent workflow

AICoder's operating model is intentionally stricter than “generate code and hope”:

```text
understand
  → inspect current state
  → create backup / recovery point
  → implement
  → test
  → verify the requested outcome
  → document / preserve reusable evidence
```

Important consequences:

- current code, runtime state, logs and tests beat stale documentation or remembered context;
- repository state is inspected before mutation;
- unrelated working-tree changes are preserved;
- risky changes require a recovery path first;
- a command returning exit code 0 is not sufficient if the original behavior was not verified;
- generated memory is context, not authority.

## Canonical tool fabric and progressive disclosure

AICoder consumes TriForce's canonical tool metadata rather than maintaining a second semantic catalogue.

The runtime uses metadata such as:

- `x_scope` — capability ownership/security scope;
- `x_task_inventory` — semantic task grouping;
- `x_display_name` / `x_tooltip` — compact UI and model hints.

`toolbox_search` can expose inventory summaries first and activate a more specific capability set only when the task requires it. This keeps model context smaller while preserving discoverability of the complete enabled tool surface.

Canonical Helper tools use the `aihelper_*` namespace. Legacy device aliases remain policy-recognized for rolling compatibility, but canonical names stay the source of truth.

## Agent runtime

The single-agent runtime supports:

- multi-turn tool use;
- structured MCP and local capability calls;
- persistent execution plans;
- workspace/file evidence reuse;
- read-back verification;
- bounded continuation turns;
- provider-aware fallback;
- resumable/paused execution states;
- change journaling and rollback for supported reversible local operations.

The common policy is shared across GUI, CLI agent mode and direct MCP paths.

## Team Runtime

AICoder's Team Runtime is designed for substantive coding tasks that benefit from multiple independent roles.

A typical run can include:

```text
research plan
  → research workers
  → brainstorm
  → implementation plan
  → isolated coding candidates
  → merge plan
  → merged candidate
  → test plan
  → deterministic verification
  → atomic disk write
```

Current Team Runtime behavior includes:

- provider-aware worker placement;
- per-provider serialization where needed to reduce avoidable rate-limit collisions;
- multiple research/coder role slots;
- candidate recovery with write tools preserved;
- progressive tool disclosure without starving implementation capabilities;
- explicit pause/failure reasons;
- deterministic final verification;
- a machine-readable change manifest;
- atomic persistence only after verification succeeds.

### Transactional persistent write

Team workers operate in isolated RAM/disk candidate workspaces. Persistent source files are changed only during the final `atomic_disk_write` stage.

Before replacement AICoder:

- detects changed/created/deleted files;
- checks for conflicting source changes;
- backs up affected existing entries;
- rejects unsafe external-symlink persistence;
- writes within the destination directory atomically;
- verifies hashes/modes/sizes after write;
- rolls the affected set back if persistence fails or is interrupted.

## Project Memory

AICoder 1.3+ extends the existing `~/.config/ai-coder/evidence.db`; it does not create a separate local memory database.

The Project-Memory layer stores:

- current structured project entries;
- `dirty` / `clean` / `conflict` state;
- per-project sync cursor;
- preserved local/server conflict versions;
- tombstones for deletion.

Project identity prefers stable repository information rather than absolute path alone, so moving a checkout does not automatically discard its project history.

### Local-first synchronization

Before action/coding-oriented runs AICoder can fail-open sync Project Memory with TriForce, then inject only a bounded relevant subset into the agent context.

The authority order remains:

```text
1. current code / runtime evidence
2. current Project Memory
3. curated / verified feature experience
4. episodic history
```

Local Project-Memory tools include:

- `project_memory_search`
- `project_memory_store`
- `project_memory_update`
- `project_memory_list`
- `project_memory_sync`

Network or memory-backend failures do not discard dirty local state.

### Claude-Mem history

TriForce can mirror accepted Project-Memory revisions into Claude-Mem as append-only episodic history. Claude-Mem records what happened; it does not become the current-state authority.

Team Runtime also writes lifecycle checkpoints to Project Memory, allowing long-running work to be reconstructed later without relying on a complete chat transcript.

## Models, accounts and BYOK

AICoder supports both backend/account-backed routing and direct provider credentials where implemented.

Current security model includes:

- OS-keyring storage for supported BYOK credentials;
- no secret values in normal state/history/log output;
- provider availability and auth diagnostics;
- explicit primary/fallback selection;
- no automatic mutation retry after ambiguous transport failure;
- provider-native tool calling where supported, with compatible normalization elsewhere.

Useful commands include:

```bash
aicoder login
aicoder whoami
aicoder models
aicoder providers
aicoder credentials status
aicoder credentials set <provider>
aicoder status
```


## System diagnostics and frozen-build safety

AICoder 1.4.2/1.4.3 hardened the boundary between a frozen PyInstaller application and native host tools.

- external system subprocesses restore the host loader environment instead of inheriting PyInstaller's private `_MEI` library path;
- Local OS diagnostics and the system-log monitor use the sanitized subprocess environment;
- native tools can resolve from standard administration paths such as `/usr/sbin` and `/sbin` even when a GUI/headless PATH omits them;
- `aa-status` / `apparmor_status` are classified as read-only diagnostics and do not trigger mutation approval or giant workspace backups;
- AppArmor denials are deduplicated by meaningful security semantics rather than volatile audit serial/PID noise;
- automatic log-analysis cooldown applies to repeated equivalent events;
- required command-runner fallback backups are bounded by the tool timeout and fail closed if the snapshot cannot be completed safely.

These changes keep diagnostics useful in standalone builds without weakening the mutation/backup policy.

## Common workflows

### Interactive agent

```bash
aicoder agent "inspect this repository, create a recovery point, fix the issue and run the relevant tests"
```

### Chat / single-shot

```bash
aicoder ask "explain the current project architecture"
aicoder chat
```

### Workspace analysis

```bash
aicoder workspace
aicoder status
```

### MCP discovery and calls

```bash
aicoder mcp --help
aicoder mcp-list
```

### Team configuration

```bash
aicoder team status
aicoder team models
aicoder team mode auto
```

### Settings / diagnostics

```bash
aicoder settings list
aicoder settings doctor
aicoder providers
aicoder systemlog status
```

## Install / run

### Development checkout

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/aicoder --help
```

### Release artifacts

Published releases are available at:

https://github.com/derleiti/ai-coder/releases/latest

Release pipelines can publish Linux standalone binaries, Debian/Ubuntu packages, Windows artifacts, Arch/AUR metadata and Termux-oriented bundles depending on the release.

## Development and verification

```bash
.venv/bin/python -m compileall -q aicoder tests
.venv/bin/python -m pytest -q
git diff --check
```

AICoder's regression suite covers tool policy, MCP compatibility, provider behavior, transactional workspace safety, Team Runtime, packaging, memory sync and failure/recovery paths.

## Local state

Typical local state lives under:

```text
~/.config/ai-coder/
  session.json       authentication/session metadata
  state.json         model/runtime/workspace settings
  evidence.db        file/failure/feature + Project Memory evidence
```

Secrets intended for BYOK provider access are stored through the supported OS keyring path rather than written into ordinary state files.

## Documentation

- `CHANGELOG.md` — release history and current release feature changes
- `docs/architecture.md` — runtime, tool fabric, Team Runtime and Project Memory architecture
- `docs/AI_CHANGELOG.md` — implementation records that do not belong in a more specific document
- `EXPERIMENTAL_RAM_RUNTIME.md` — experimental runtime design/background
- `SECURITY.md` — security and private disclosure policy
- `CONTRIBUTING.md` — contribution workflow
- `SUPPORT.md` — support and licensing contacts

## License

AILinux-authored material first published under the current licensing model is covered by the **AILinux Proprietary Source License**. Earlier AICoder versions were published under MIT; those historical grants remain valid. See `LICENSE` and `LICENSE-HISTORY.md`.

Commercial / redistribution / OEM licensing: `support@ailinux.me`.
