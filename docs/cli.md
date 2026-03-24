# CLI-Referenz — ai-coder

## Alle Commands

### Auth

```bash
aicoder login [--base-url URL] [--email EMAIL] [--password PASS]
aicoder logout
aicoder whoami          # Token-Payload anzeigen
aicoder handshake       # Tools-Liste (ungefiltert)
aicoder tools           # Kurzliste der erlaubten Tools
aicoder profile         # Lokale Session (masked)
```

### Session State

```bash
aicoder model                              # anzeigen
aicoder model anthropic/claude-sonnet-4    # setzen

aicoder fallback                           # anzeigen
aicoder fallback gemini/gemini-2.0-flash   # setzen

aicoder swarm                # anzeigen
aicoder swarm auto           # setzen: off | auto | on | review

aicoder status               # Übersicht: model, fallback, swarm, workspace, docs
```

State wird gespeichert in `~/.config/ai-coder/state.json`.

### Workspace

```bash
aicoder workspace [path]
```

Erstellt Git-Repo-Snapshot. Persistiert `workspace_root` in state.json.

### LLM — ask / chat / task / review

```bash
# Single-shot
aicoder ask "Frage"
aicoder ask "Frage" --model groq/llama-3.3-70b-versatile
aicoder ask "Frage" --no-agents        # ohne AGENTS.md als system_prompt
aicoder ask "Frage" --temperature 0.3 --max-tokens 2048

# Interaktiv
aicoder chat
aicoder chat --model groq/llama-3.3-70b-versatile

# In-session commands: /model <n>  /swarm <mode>  /status  /clear  /exit

# File-aware task
aicoder task "Füge Docstrings hinzu" -f datei.py
aicoder task "Refactor X" -f datei.py --apply       # Diff + y/N → schreiben
aicoder task "Refactor X" -f datei.py --dry-run     # Diff, nicht schreiben
aicoder task "..." -f a.py -f b.py                  # mehrere Dateien (kein apply)

# Code Review
aicoder review -f datei.py
aicoder review -f datei.py --model groq/llama-3.3-70b-versatile
```

Vor jedem LLM-Call wird angezeigt: `model=... fallback=... swarm=...`  
AGENTS.md wird automatisch als system_prompt geladen (wenn vorhanden).

### Modelle & MCP

```bash
aicoder models                    # alle verfügbaren Modelle
aicoder models --filter groq      # gefiltert
aicoder models --json             # als JSON

aicoder mcp-list                  # alle MCP-Tools tabellarisch
aicoder mcp health                # MCP health check
aicoder mcp <tool> [key=val ...]  # beliebiger MCP-Tool-Call
```

### History

```bash
aicoder hist          # letzte 10 Einträge
aicoder hist -n 20    # letzte 20
aicoder hist --clear  # History löschen
```

Gespeichert in `~/.config/ai-coder/history.json`, max 50 Einträge.

## Vollständige Command-Liste

```
login       logout      whoami      handshake   tools       profile
workspace   mcp         mcp-list    models
model       fallback    swarm       status
ask         chat        task        review
hist        status-demo
```

## Swarm-Modi

| Modus | Verhalten |
|---|---|
| `off` | Kein Swarm (default) |
| `auto` | Swarm-Spinner bei MCP-Calls |
| `on` | Swarm-Spinner immer |
| `review` | Hive-Spinner |

Echter paralleler Swarm-Call: V3 (siehe docs/swarm.md).
