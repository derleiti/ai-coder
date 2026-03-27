## [0.6.0] - 2026-03-27

### Security
- **KRITISCH**: `cmd_sudo` überträgt Passwort nicht mehr über Netzwerk — läuft jetzt lokal via subprocess
- **HOCH**: `--password` CLI-Argument bei `login` entfernt — verhindert Credential-Leak in Shell-History
- **HOCH**: SSL-Fallback zu `CERT_NONE` entfernt — kein stilles MITM-Risiko mehr

### Fixed
- `build_parser()`: Duplikat-Subparser-Block entfernt (Dead Code nach cmd_hist)
- `agent.py` / `local_exec`: Destructive-Pattern-Guard eingebaut (rm -rf, dd, mkfs → User-Confirmation)

### Performance
- `session_state.py`: In-memory Cache für State-File — vermeidet Disk-Reads im Agent-Loop

### Packaging
- Package-Name vereinheitlicht auf `aicoder` (war: `ai-coder` in pyproject.toml)
- README: Veralteten NSIS v0.4.0-Hinweis entfernt

# CHANGELOG — ai-coder

## v0.3.1 (2026-03-25)

### Neu
- Interaktiver Model-Picker:  im REPL öffnet TUI-Browser
  - ← → Pfeiltasten: Provider durchschalten (anthropic/gemini/mistral/groq/...)
  - ↑ ↓ Pfeiltasten: Modell in der Liste wählen
  - Enter: bestätigen | q/ESC: abbrechen
  - Zeigt live Modellanzahl pro Provider, Capabilities, Scroll-Fenster
- Setup-Wizard nutzt ebenfalls den interaktiven Picker
- client.py:  Methode ergänzt

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
