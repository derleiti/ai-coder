# Backend-Scope — ai-coder

## Problem

Aktuell: Login über `/v1/auth/login` ergibt ein allgemeines Token.  
`/v1/auth/client/handshake` gibt alle Tools für die User-Rolle zurück.  
ai-coder bekommt damit zu viele Tools — inkl. Admin/Ops-Rechte.

## Ziel

ai-coder soll einen eigenen eingeschränkten Scope haben:

**Erlaubt:**
- code_read, code_search, code_tree, code_edit
- dev_analyze, dev_debug, dev_lint, dev_refactor, dev_summarize
- file_ops (read/list/find — kein shell-write ohne Review)
- search (kanonische Websuche), web_fetch_local (lokaler URL-Abruf)
- memory_search, memory_store (user-scoped)
- git_ops (read: status, log, diff — kein push)

**Verboten:** alle Ops/Admin/Infra-Tools (siehe security.md)

## Mögliche Umsetzung im Backend

### Option A: User-Agent basiert

TriForce erkennt `User-Agent: ai-coder/...` und wendet ein Coding-Profil an.

Wo: `routes/mcp.py` oder `auth/handshake`-Handler  
Logik: wenn `user_agent.startswith("ai-coder")` → `tool_filter = CODING_SCOPE`

### Option B: client_profile im Token

Login-Request mit `{"client_profile": "ai_coder"}`.  
Backend legt beim JWT-Signing das Profil fest.  
Handshake gibt nur Coding-Tools zurück.

Wo: `routes/auth.py` login-Handler + JWT-Payload + handshake-Handler

### Option C: Separates API-Key-Profil

Eigenes API-Key-Tier `ai_coder` in `config/users.json` oder RBAC.

### Empfehlung

Option A ist schnell umsetzbar ohne Breaking Changes.  
Option B ist sauberer für Multi-Client-Szenarien.

## Status

Der lokale Client erzwingt seit der Security-Härtung eine zentrale Coding-
Allowlist für GUI, Agent und direkte MCP-Aufrufe. Der Backend-Status ist in
diesem Repository nicht verifizierbar. Serverseitiges `client_profile=ai_coder`
bleibt erforderlich, damit manipulierte oder ältere Clients ebenfalls nur den
minimalen Scope erhalten.
