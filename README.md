# AICoder

[![CI](https://github.com/derleiti/ai-coder/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/derleiti/ai-coder/actions/workflows/ci.yml)
[![Security](https://github.com/derleiti/ai-coder/actions/workflows/security.yml/badge.svg?branch=master)](https://github.com/derleiti/ai-coder/actions/workflows/security.yml)

**Current release: 1.2.6** · AILinux coding/DevOps agent for TriForce and the Loom capability fabric.

AICoder is the worker-facing client in the AILinux family. It combines a terminal/REPL and PyQt6 desktop UI with a guarded tool loop, local workspace execution, remote MCP capabilities, provider/account integrations, recovery backups, and resumable agent runtime state.

## Current architecture

- **TriForce** is the canonical remote control plane and MCP schema authority.
- **AICoder** is the coding/automation worker and can execute approved local capabilities.
- **AILinux Loom** projects canonical capabilities and execution targets into one fabric.
- **AILinux Helper** exposes user-consented endpoint/workspace/device capabilities.
- Shared tool names inherit canonical TriForce schemas; execution location is a target/policy decision rather than a second tool contract.
- Destructive workspace operations use the shared `.workspacebackup` recovery convention.

## Highlights in the current master

- ChatGPT/Codex account login with single-flight fallback and quota diagnostics.
- Canonical tool projection for shared TriForce/AICoder capabilities.
- Local Git tool supporting read and explicitly approved write modes.
- Optional Loom disposable-container execution without giving the model Docker-socket access.
- Dark/light GUI tokens sourced from the Loom design language.
- Persistent agent plans, runtime recovery, feature memory and regression-tested backup behavior.

## Install / run

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/aicoder
```

Release artifacts are published at https://github.com/derleiti/ai-coder/releases/latest. The current release line includes Linux, Debian/Ubuntu, Windows and Termux-oriented artifacts where available.

## Common commands

```bash
aicoder login
aicoder status
aicoder models
aicoder chat
aicoder agent "inspect this repository and run the relevant tests"
aicoder mcp <tool> [args]
```

## Development and verification

```bash
.venv/bin/python -m compileall -q aicoder tests
.venv/bin/python -m pytest -q
```

CI currently tests supported Python versions on GitHub and runs a separate security workflow.

## Documentation

- `CHANGELOG.md` — release history
- `docs/architecture.md` — architecture details
- `EXPERIMENTAL_RAM_RUNTIME.md` — experimental runtime design
- `SECURITY.md` — private disclosure policy
- `CONTRIBUTING.md` — contribution workflow
- `SUPPORT.md` — support and licensing contacts

## License

AILinux-authored material first published under the current licensing model is covered by the **AILinux Proprietary Source License**. Earlier AICoder versions were published under MIT; those historical grants remain valid. See `LICENSE` and `LICENSE-HISTORY.md`.

Commercial / redistribution / OEM licensing: `support@ailinux.me`.
