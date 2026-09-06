## Unreleased

- Make Team merge pauses diagnosable and autonomously resumable in the same integration workspace; preserve the real pause/failure reason instead of collapsing it to `merge failed`.
- Fix active project selection so GUI workspace changes take effect without restart; distinguish `projects_root` (default `~/workspace`) from the concrete `workspace_root`.
- Prevent Team Runtime from treating the projects container as a project and make fallback verification safe for fresh non-Git projects.

- Prevent transactional RAM commits from following a replaced directory symlink
  outside the source workspace; reject newly persisted external symlinks.
- Add durable atomic file installation, post-commit fingerprint verification and
  complete cleanup after rollback, including cancellation during commit.
- Add agent/team run IDs, deterministic terminal events, post-cleanup team
  completion reporting and a machine-readable final change manifest.
- Prefer an explicitly selected workspace's `AGENTS.md` over unrelated ancestor
  repository instructions.

## v1.2.3 (2026-09-04)

- Fix Debian/Ubuntu upgrades where a stale `~/.local/bin/aicoder` pip launcher shadows the packaged `/usr/bin/aicoder`; the package now backs up and removes only a known broken legacy launcher.
- Include package metadata in PyInstaller builds so standalone Linux/Windows binaries report the real release version instead of `0+unknown`.
- Fix checked-in AUR `.SRCINFO` release source metadata.
- Add release verification for packaged version reporting.

## v1.2.2 (2026-09-04)

- Complete BYOK routing across `ask`, `chat`, `task`/`review`, agent, GUI and team runtimes.
- Add native Anthropic Messages API transport for securely stored Anthropic keys.
- Add CLI OS-keyring management with `aicoder credentials set|delete <provider>`.
- Keep provider secrets out of state, history and logs; preserve TriForce fallback when no direct key is configured.

## v1.2.1 (2026-09-04)

- Fix missing typing import detected by release CI.
- Restore audit redaction import in team debug logging to prevent a runtime NameError while sanitizing string diagnostics.
- Add regression coverage for inline secret redaction.

## v1.2.0 (2026-09-04)

- Add transactional RAM workspace backend and staged multi-agent team runtime.
- Add team orchestration, blind merge handoffs, candidate recovery, and fresh-test evidence gates.
- Add runtime performance telemetry and stronger diagnostics for long-running agent workflows.
- Harden merger behavior, model/tool compatibility, unlimited scheduling, cleanup, and auto-resume handling.
- Add secure per-provider API credential storage and direct-provider transport groundwork.
- Expand regression coverage for team runtime, workspace isolation, provider credentials, performance, and recovery.

## v1.0.1 (2026-08-24)

### Release infrastructure
- Fix GitHub Actions release metadata validation and version synchronization.

## v1.0.0 (2026-08-23)

### Features
- ✅ Terminal Coding Agent für AILinux/TriForce
- ✅ Multi-Modell-Support (600+ LLMs über 9 Provider)
- ✅ PyQt6 GUI mit System-Tray-Integration
- ✅ Autonomer Agent mit MCP-Tool-Loop
- ✅ Workspace-Snapshot & lokale Dateiverwaltung
- ✅ Swarm-Modus (auto/review/on/off)
- ✅ Cross-platform: Linux, Windows, macOS, Android/Termux

### Platforms & Binaries
- 🐧 Linux Binary (PyInstaller) + .deb Package
- 🪟 Windows .exe + NSIS Installer
- 🏗️ Arch/AUR PKGBUILD
- 🤖 Android/Termux Bundle + Installer Script

### Installation
- **Linux:** `sudo dpkg -i aicoder_1.0.0_amd64.deb` or binary download
- **Windows:** NSIS Installer or standalone exe
- **Arch/AUR:** `yay -S aicoder`
- **Termux:** Dedicated installer script

### Build Pipeline
- Unified CI/CD: Linux, Windows, Android in single workflow
- Version consistency across all components (pyproject.toml, control, PKGBUILD, NSIS)
- Automated SHA256 checksums
- GitHub Release automation

### Stability & Security
- Tool Policy enforcement (coding-only MCP allowlist)
- Audit logging for sensitive operations
- Privilege Broker for elevated tasks
- No breaking changes from 0.9.x

## v0.9.8b1 — Directory Tool Beta (2026-08-20)

- Add a typed `directory_create` local capability for creating project directories and missing parents.
- Make `file_edit` explicitly file-only so agents no longer misuse it for folder creation.
- Apply workspace-boundary approval and mutation classification to directory creation.
- Preserve dynamic "all tools" selection in the GUI so newly added safe capabilities become available automatically.
- Unify package metadata at `0.9.8b1` and make the GitHub release workflow prerelease-aware.

## v0.9.6 — Unified Tool Policy and MCP Hardening (2026-08-13)

- Expose only the canonical MCP `search` tool; remove the obsolete DuckDuckGo
  local search and the synthetic `web_search` alias from model-facing tools.
- Enforce one coding-only tool policy across CLI agent, GUI, direct MCP calls,
  and the low-level MCP client; forbidden admin/ops/shell scopes fail closed.
- Replace model-facing local shell commands with typed, workspace-confined file,
  tree, search, edit, and read-only Git capabilities.
- Normalize JSON-RPC errors, MCP `isError`, multi-block content and
  `structuredContent`; disable transport retries for tool calls and prevent
  mutation retries after ambiguous timeouts.
- Mark tool output as untrusted model input, redact nested audit secrets, use
  private atomic config writes, and protect SQLite chat-history permissions.
- Make fallback reporting deterministic and align `on`/`review` swarm behavior
  with the documented operator/advisor hierarchy.
- Add end-to-end regression coverage for policy bypasses, typed local tools,
  MCP protocol variants, retry safety, fallback, swarm, and redaction.
- Keep client and TriForce on one tested, restrictive `ai-coder` MCP contract;
  alias normalization, Swarm calls, and login role fields now match end to end.
- Migrate the obsolete persisted 40-tool snapshot to the current dynamic
  coding-only catalog so newly supported safe tools are visible after upgrade.

## v0.9.5 — Agent Reliability + Long Task Timeout (2026-08-08)

- Harden tool-call parsing for provider responses that omit only trailing JSON object braces inside a complete `<tool_call>` envelope.
- Keep genuinely truncated tool calls non-executable instead of guessing missing content.
- Guide large `file_edit` writes into smaller sequential chunks to avoid oversized heredoc generations.
- Enforce primary/fallback separation and improve fallback/tool compatibility behavior.
- Raise the default AICoder model request timeout to 300 seconds for long coding and agent tasks.
- Expand the GUI timeout control to 300 seconds and keep release metadata aligned across Debian, AUR, and the Windows installer.

## v0.9.2 — Primary Routing + MCP Code Handler Fix (2026-08-08)

- AI Coder keeps the selected primary model for quick chat; fallback is only used after failure or explicit loop recovery.
- Updated routing regression tests for CLI and GUI semantics.
- TriForce MCP V4 code handlers now use the maintained `mcp_service` implementations instead of stale `tristar_mcp` imports.
- NVIDIA free-model routing remains server-side via the unified provider router.

## v0.9.1 — GUI Approval Broker + Tool Loading Hardening (2026-07-16)

- Preserve complete structured MCP arguments and mutation/destruction metadata through the GUI approval signal.
- Display redacted tool arguments in approval dialogs and default state-changing operations to deny.
- Harden provider tool-call normalization, account-scoped tool caching, and on-demand tool loading behavior.
- Add regression coverage for GUI approval metadata, secret redaction, provider compatibility, and tool selection.

## v0.9.0 — Reliable Agent Runtime + Privilege Broker (2026-07-15)

### Agent Runtime
- Persistent REPL conversation context across follow-up prompts.
- Progress-aware long-running agent loop with checkpoints, stagnation detection,
  automatic fallback switching, and no fixed 30-step interruption.
- Provider-compatible native and text tool-call normalization.
- Correct local `~` path expansion without weakening `shell=False` execution.

### Security and UX
- Explicit local approval broker for writes, deletion, protected paths, and sudo.
- Password handling remains entirely inside the operating system's sudo/Polkit flow.
- Prompt-Toolkit REPL with multiline input, history, safe repaint, and `/new`.
- Improved PyQt6 GUI tool selection, model controls, and agent status feedback.
