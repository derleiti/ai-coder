# Architektur — ai-coder

## Überblick

```
[Terminal / User]
      │
      ▼
[ai-coder CLI]          ← dünner lokaler Client
      │                    Python, keine externen Abhängigkeiten zur Runtime
      │  HTTP/JSON-RPC 2.0
      ▼
[TriForce Backend]      ← Intelligenz sitzt hier
  /v1/auth/*              FastAPI, uvicorn, Apache Proxy
  /v1/mcp                 600+ Modelle, 9 Provider
      │
      ▼
[LLM Provider]
  Anthropic / Gemini / Ollama / Groq / ...
```

## Lokale Dateien

```
~/.config/ai-coder/
  session.json    ← Login-Token, user_id, tier, account_role
  state.json      ← selected_model, fallback_model, swarm_mode, workspace_root
```

## Module

| Modul | Zweck |
|---|---|
| `cli.py` | Argument-Parser, Command-Handler |
| `client.py` | HTTP-Client gegen TriForce API |
| `config.py` | Session-Persistenz |
| `session_state.py` | Modell/Swarm-State-Persistenz |
| `docs_context.py` | Projekt-Doku-Discovery (AGENTS.md, README, ...) |
| `workspace.py` | Git-Repo-Snapshot |
| `status.py` | Terminal-Spinner, Phase-Labels |

## API-Endpunkte

| Zweck | Methode | Pfad |
|---|---|---|
| Login | POST | /v1/auth/login |
| Verify | GET | /v1/auth/verify |
| Handshake | GET | /v1/auth/client/handshake |
| MCP-Call | POST | /v1/mcp |

## MCP-Call Format (JSON-RPC 2.0)

```json
{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "params": {
    "name": "tool_name",
    "arguments": {}
  },
  "id": 1
}
```
