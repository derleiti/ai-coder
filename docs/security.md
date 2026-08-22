# Sicherheit — ai-coder

## Grundregeln

- Backend-MCP bleibt auf den kanonischen AICoder-Scope begrenzt; ein Admin-Login erweitert diesen Modell-Scope nicht
- Lokale Systemdiagnostik ist über typisierte, read-only Local-OS-Tools erlaubt
- Lokale Mutationen und erhöhte Rechte laufen ausschließlich durch Tool-Policy + PrivilegeBroker
- Read-first: Dateien lesen vor Schreiben
- Keine destruktiven Ops ohne explizite Bestätigung
- Modell-Tools werden durch eine zentrale Allowlist technisch erzwungen
- Lokale Dateiwerkzeuge sind auf `workspace_root` begrenzt; ein Scope-Escape braucht explizite Freigabe
- `shell`/`binary_exec` sind lokale Runtime-Tools, keine frei an den Backend-MCP weitergereichten Admin-Werkzeuge
- Root-/Security-Änderungen werden niemals automatisch freigegeben

## Gespeicherte Credentials

```
~/.config/ai-coder/session.json   chmod 600
~/.config/ai-coder/state.json     chmod 600
```

Token wird nie geloggt. Bei `profile`-Command: maskiert.

## Backend-MCP-Scope

Direkte Backend-MCP-Aufrufe bleiben fail-closed auf der kanonischen Allowlist.
Nicht freigegebene Admin-, Vault-, Mail-, Notification-, Restart-, Remote- oder
sonstige Infrastruktur-Werkzeuge werden vor dem Netzwerk blockiert.

Lokale Runtime-Tools sind davon getrennt: typisierte Workspace-Tools, read-only
Local-OS-Diagnostik und ausdrücklich aktivierte lokale Ausführung werden auf dem
Client geroutet und durch Workspace-Grenzen, Security-Metadaten, Audit und
PrivilegeBroker abgesichert.

## Backend-Scope (TODO)

Aktuell ergibt Login einen vollen Client-Token mit Zugriff auf alle Tools.  
Ziel: ai-coder soll als eigener `client_profile = ai_coder` laufen.  
Details: `docs/backend_scope.md`

Der Client erzwingt seinen Coding-Scope zusätzlich lokal. Das ersetzt keine
serverseitige Autorisierung, verhindert aber, dass ein Modell, ein Text-Parser
oder ein direkter CLI-Aufruf verbotene Toolnamen an `/v1/mcp` weiterleitet.
