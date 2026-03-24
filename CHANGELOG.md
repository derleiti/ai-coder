# CHANGELOG — ai-coder

## v0.3.0 (2026-03-24)

### Neu
- `ask` — Single-shot LLM mit AGENTS.md als system_prompt
- `chat` — Interaktive Multi-Turn-Session, in-session /model /swarm /status /clear
- `task` — File lesen → LLM → farbiger Diff → optional apply (y/N)
- `review` — Strukturiertes Code-Review (Bugs/Security/Perf/Quality)
- `models [--filter X]` — Verfügbare Modelle vom Backend (659)
- `mcp-list` — Alle MCP-Tools tabellarisch
- `hist [-n N] [--clear]` — Call-History (ask/chat/task) persistent
- `session_state.py` — model/fallback/swarm/workspace in state.json
- `docs_context.py` — AGENTS.md + doc-Discovery
- `history.py` — Persistente History, max 50 Einträge
- `task.py` — File-aware Task Runner

### Geändert
- `client.py` — `chat()` mit automatischem Fallback-Modell bei Fehler
- `status.py` — Spinner auf stderr (stdout sauber für JSON-Piping)
- `client_auth.py` — Backend-Scope: 288 → 28 Tools via User-Agent Filter
- `client_mcp.py` — CODING_SCOPE_TOOLS definiert

### Fixes
- Spinner-Output mischt sich nicht mehr mit JSON (stderr/stdout getrennt)
- `build_parser()` return-Bug behoben
- `swarm` NameError in cmd_mcp behoben

## v0.2.0 (initial)
- login, logout, whoami, handshake, tools, profile
- workspace, mcp
- model, fallback, swarm, status
