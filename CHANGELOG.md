# CHANGELOG — ai-coder

## v0.3.8 (2026-03-24)

### Neu
- **Windows-Support:** `aicoder.exe` (13 MB Standalone, PyInstaller Onefile)
- **GitHub Actions CI:** Automatischer Windows-Build bei Tag-Push (`v*`)
- **NSIS-Installer:** Setup mit PATH-Integration und Add/Remove Programs
- **Plattform-übergreifend:** Eine Codebase für Linux + Windows

### Geändert
- PyInstaller Hidden Imports: alle 13 aicoder-Module explizit deklariert
- `.venv/` und `ai_coder.egg-info/` aus Git-Tracking entfernt
- Verify-Step in CI: diagnostische Ausgabe statt hartem Fail

### CI/CD
- `.github/workflows/build-windows.yml` — Windows Build Pipeline
- `packaging/windows/installer.nsi` — NSIS Installer Script
- `packaging/windows/add-to-path.ps1` — PATH-Setup Helper
- Automatische GitHub Releases mit Binary-Assets

## v0.3.0 (2026-03-24)

### Neu
- `ask` — Single-shot LLM mit AGENTS.md als system_prompt
- `chat` — Interaktive Multi-Turn-Session, in-session /model /swarm /status /clear
- `task` — File lesen → LLM → farbiger Diff → optional apply (y/N)
- `review` — Strukturiertes Code-Review (Bugs/Security/Perf/Quality)
- `agent` — Autonomer Agent mit Tool-Loop (opencode-Style UI)
- `models [--filter X]` — Verfügbare Modelle vom Backend (659)
- `mcp-list` — Alle MCP-Tools tabellarisch
- `hist [-n N] [--clear]` — Call-History (ask/chat/task) persistent
- `session_state.py` — model/fallback/swarm/workspace in state.json
- `docs_context.py` — AGENTS.md + doc-Discovery
- `history.py` — Persistente History, max 50 Einträge
- `task.py` — File-aware Task Runner
- `ui.py` — opencode-inspiriertes Terminal-UI (Braille-Spinner, Panels, ANSI)

### Geändert
- `client.py` — `chat()` mit automatischem Fallback-Modell bei Fehler
- `status.py` — Spinner auf stderr (stdout sauber für JSON-Piping)

## v0.2.0 (initial)
- login, logout, whoami, handshake, tools, profile
- workspace, mcp
- model, fallback, swarm, status
