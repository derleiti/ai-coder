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
  → zentrale lokale Risiko-/Tool-Policy (Backend-RBAC + per-run capability subset)
  → lokale typisierte Workspace-Capability ODER JSON-RPC /v1/mcp
  → als untrusted markiertes Tool-Ergebnis
  → nächster Operator-Turn
```

GUI, CLI-Agent und direkte `aicoder mcp`-Aufrufe verwenden dieselbe Policy.
Lokale typisierte Workspace-Tools, progressive Capability-Discovery und der
Local-OS-Provider und lokale Runtime-Tools laufen clientseitig. Backend-Tools werden aus dem authentisierten TriForce-Katalog übernommen statt durch eine zusätzliche Coding-only-Allowlist beschnitten.
TriForce ist dabei ausschließlich Backend-Service und niemals Operator-Ziel: Host-/Repository-/Service-/Container-/Remote-Admin-Fähigkeiten des TriForce-Hosts werden aus dem AICoder-Katalog entfernt und am MCP-Transport nochmals blockiert. Lokale gleichnamige Workspace-Tools bleiben verfügbar.
Lokale und MCP-gestützte Mutationen, Workspace-Escapes, Elevation, destruktive Aktionen und Security-Änderungen werden transportunabhängig vom PrivilegeBroker bzw. der zentralen Approval-Policy klassifiziert.

### Tool-Protokoll, Fortsetzung und Verifikation

Das modellseitige Text-Protokoll bevorzugt vollständige `TOOL_CALL ... END_TOOL_CALL`-Blöcke ohne Prosa. Mehrere unabhängige Blöcke dürfen in einem Turn gebündelt werden. Die Runtime toleriert vollständige valide Blöcke mit gewöhnlicher Begleitprosa als Provider-Recovery, führt jedoch keine gefenceten Dokumentationsbeispiele und keine teilweise/malformed Sequenz aus.

Im opt-in nativen OpenRouter-Modus wird der Provider-Verlauf nativ erhalten: `assistant.tool_calls` wird von `role: tool` mit passender `tool_call_id` beantwortet. Text-Toolresultate werden dort nicht als künstliche User-Nachricht dupliziert.

Fortschritt und Sicherheitsrisiko sind getrennte Zustände. `PrivilegeBroker` darf Tests/Shell konservativ als potentiell mutierend klassifizieren; die Agent-Runtime entscheidet separat, ob ein Aufruf Implementation, Verifikation oder reine Inspektion war. Deterministischer Read-back bestätigt Daten-/Konfigurationsartefakte. Code-/Verhaltensänderungen benötigen weiterhin einen geeigneten ausführbaren Check. Diese Progress-Semantik gilt identisch mit und ohne persistenten `AgentPlan`.

Continuation-Turns bekommen ein kleineres eigenes Timeout als der initiale Planungsturn; bekannte Reasoning-Modelle erhalten ein höheres, aber weiterhin begrenztes Continuation-Budget. Unveränderte File-Evidence wird über normalisierte absolute Workspace-Pfade wiederverwendet.

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
| `local_os.py` | Typisierte Local-OS-Diagnostik und System-Capabilities |
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

Der Client sendet bei allen Requests `X-Client-Profile: ai-coder`. Das Profil identifiziert den Operator-Client, gewährt aber selbst keine zusätzlichen Rechte. Autorisierung kommt aus dem angemeldeten Backend-Konto/RBAC; der Client übernimmt den angebotenen Tool-Katalog und erzwingt lokal Workspace-, Risiko-, Approval- und Privilege-Grenzen.

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
