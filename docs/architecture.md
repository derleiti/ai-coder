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

## Agent-/Tool-Datenfluss

```text
User prompt
  → /v1/client/chat (Systemregeln + aktivierte Tool-Schemas)
  → native oder kompatibel normalisierte Tool-Calls
  → zentrale lokale Tool-Policy (deny forbidden + per-run allowlist)
  → lokale typisierte Workspace-Capability ODER JSON-RPC /v1/mcp
  → als untrusted markiertes Tool-Ergebnis
  → nächster Operator-Turn
```

GUI, CLI-Agent und direkte `aicoder mcp`-Aufrufe verwenden dieselbe Policy.
Lokale typisierte Workspace-Tools, progressive Capability-Discovery und der
read-only Local-OS-Provider laufen clientseitig. Backend-Tools außerhalb der
kanonischen AICoder-Allowlist werden weiterhin vor dem Netzwerk blockiert.
Lokale Mutationen, Workspace-Escapes und Elevation werden transportunabhängig
vom PrivilegeBroker klassifiziert und freigegeben oder fail-closed blockiert.

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
| `workspace.py` | Aktiver Workspace und Scope-Grenzen |
| `capabilities.py` | Progressive Capability-Auswahl und Dynamic Expansion |
| `plugins.py` | Plugin-/ToolProvider-Registry |
| `local_os.py` | Typisierte read-only Local-OS-Diagnostik |
| `privileges.py` | Zentrale Risiko- und Freigabe-Policy |
| `mcp_server.py` | Lokales MCP-Serving für freigegebene Provider |
| `optimizer.py` | Evidenzbasierte Optimierungsplanung |
| `change_journal.py` | Privates strukturiertes Änderungsjournal |
| `status.py` | Terminal-Spinner, Phase-Labels |

## API-Endpunkte

| Zweck | Methode | Pfad |
|---|---|---|
| Login | POST | /v1/auth/login |
| Verify | GET | /v1/auth/verify |
| Handshake | GET | /v1/auth/client/handshake |
| MCP-Call | POST | /v1/mcp |

Der Client sendet bei allen Requests `X-Client-Profile: ai-coder`. Das Backend
verwendet dieses Profil ausschließlich zur Einschränkung: `tools/list` und
`tools/call` werden auf denselben kanonischen Coding-Tool-Vertrag begrenzt, den
der Client lokal nochmals durchsetzt. Das Profil gewährt keine zusätzlichen
Rechte.

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

MCP-Tool-Aufrufe werden auf Transportebene nicht automatisch wiederholt.
Read-only-Aufrufe dürfen im Executor einmal wiederholt werden; mutierende Aufrufe
nie, solange das Backend keinen Idempotency-Key-Vertrag bereitstellt. JSON-RPC
`error`, MCP `isError`, mehrere Textblöcke und `structuredContent` werden
normalisiert ausgewertet.
