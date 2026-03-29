## v0.6.5 — Security Hardening + Shared Executor (2026-03-29)

### Security Fixes (Critical)
- **GUI Command Approval**: Every `local_exec` call now shows a confirmation dialog before execution. Destructive commands default to "No".
- **Stop Button**: Agent loop can be interrupted at any point between iterations or tool calls.
- **Audit Log**: All tool executions (local_exec + MCP) are logged to `~/.config/ai-coder/audit.jsonl` with timestamp, command, result, duration, and model.
- **Token Expiry Handling**: Pre-flight JWT expiry check prevents requests with expired tokens. New `TokenExpiredError` gives clear guidance ("aicoder setup").
- **Destructive Pattern Detection**: Extended pattern list (15 patterns) including `wipefs`, `shred`, `truncate`, `mv /`.

### Architecture Refactor
- **New: `executor.py`** — Shared execution engine used by both CLI and GUI. Eliminates code duplication for: tool parsing, local/MCP execution, message management, destructive guards, audit logging.
- **New: `audit.py`** — Persistent JSONL audit log for all tool calls. Includes `get_recent()` and `get_local_exec_history()` for programmatic access.
- **Refactored: `agent.py`** — Now a thin CLI wrapper around executor. Re-exports constants for backwards compatibility.
- **Refactored: `chat_widget.py`** — Complete rewrite using executor, with approval dialog, stop button, and model selector.

### GUI Features
- **Model Selector Dropdown**: Editable combo boxes for primary model and fallback directly in chat tab. Priority: combo > settings > state file.
- **Enhanced Status Bar**: Shows user, tier, token expiry countdown, workspace name, tool count. Color changes based on token status (green/orange/red).
- **XML Tool-Call Parsing**: Fixed parsing for models that return `<n>tool</n><arguments>` format (DeepSeek, etc.).

### Client Improvements
- `client.py`: New `token_expires_in()`, `is_token_expired()`, `token_status()` methods.
- Pre-flight expiry check saves round-trip on expired tokens.
- HTTP 401/403 with token keywords detected as `TokenExpiredError`.
- Version bump to 0.6.5.

## v0.6.3 — Context Loss Fix (2026-03-28)

### Critical Bugfix
- **Fixed context loss in Agent-Loop (agent.py + chat_widget.py)**
  - Replaced broken string-concat history (`"User: ...\nAssistant: ..."`) with structured `messages[]` array
  - Removed 600-char truncation of assistant responses — full tool outputs preserved
  - Increased context window from 3 turns to 24 messages (12 turn-pairs)
  - System prompt + initial user message always retained during trimming

### GUI Fix (chat_widget.py)
- Added cross-turn context persistence via `self._messages`
- Worker returns updated messages array via `messages_updated` signal
- Clear/reset button properly resets message history

### Backend Fix (TriForce client_chat.py)
- Fixed expired-token fallback: was using non-existent `settings.secret_key`, now uses `JWT_SECRET`
- Expired tokens now correctly decode tier info instead of falling back to free tier

### Other
- Version bump to 0.6.2
- Updated User-Agent string

## [0.6.0] - 2026-03-27

### Security
- **KRITISCH**: `cmd_sudo` auf rein lokale subprocess-Ausführung umgestellt — Passwort verlässt nie das lokale System (vorher: Passwort im Klartext via Netzwerk an Backend-Server)
- **HOCH**: SSL-Fallback zu `CERT_NONE` entfernt — TLS-Verifikation wird nie mehr deaktiviert
- **HOCH**: `--password` CLI-Argument aus `login` entfernt — verhindert Passwort-Leak in Shell-History

### Fixed
- Duplizierten Subparser-Block in `build_parser()` entfernt (Dead Code, 14 doppelte Parser-Registrierungen)
- Package-Name von `ai-coder` auf `aicoder` vereinheitlicht (pyproject.toml, egg-info, AUR)

### Improved
- `session_state.py`: In-memory Cache für State-Reads — reduziert Disk-I/O im Agent-Loop erheblich
- `httpx` aus Dependencies entfernt (war nie genutzt, `urllib` ist ausreichend)

## v0.6.0

### Security
- **ENTFERNT: `aicoder sudo`** — Passwort wurde im Klartext über das Netzwerk an den Backend-Server übertragen. Command komplett entfernt, da Remote-sudo via MCP kein sinnvolles Modell ist.
- **`--password` Flag entfernt** aus `aicoder login` — Passwort wird jetzt ausschließlich über `getpass()` abgefragt, nie aus CLI-Args (kein Shell-History-Leak).
- **SSL: CERT_NONE Fallback entfernt** — TLS-Verifikation ist jetzt immer aktiv. Kein stiller MITM-Angriffspfad mehr.

### Fixes
- **Duplikate Subparser-Registrierungen entfernt** aus `cli.py` (Dead-Code-Block nach `cmd_hist`)
- **`httpx` aus Dependencies entfernt** — wurde nie genutzt, `urllib` ist der tatsächliche HTTP-Client
- **Package-Name vereinheitlicht** auf `aicoder` (war `ai-coder` in pyproject.toml)

### Performance
- **Session-State: Atomic Write** via tmp-file + replace in `_save_raw()` — kein partiell geschriebenes state.json mehr
- README: NSIS-Installer-Hinweis auf v0.4.0 entfernt (ist seit v0.5.x live)

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
