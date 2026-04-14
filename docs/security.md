# Sicherheit — ai-coder

## Grundregeln

- ai-coder ist ein Coding-Client, kein Admin-Client
- Auch bei Admin-Login: Scope bleibt Coding
- Read-first: Dateien lesen vor Schreiben
- Keine destruktiven Ops ohne explizite Bestätigung

## Gespeicherte Credentials

```
~/.config/ai-coder/session.json   chmod 600
~/.config/ai-coder/state.json     chmod 600
```

Token wird nie geloggt. Bei `profile`-Command: maskiert.

## Verbotene Tool-Scopes

ai-coder darf folgende Backend-Tools NICHT aufrufen:
- `admin_*` — Admin-Ops
- `vault_*` — Secrets/Keys
- `mail_*` — E-Mail
- `notify_*` — Notifications
- `restart_*` / `service_*` — Service-Management
- `remote_*` — Remote-Execution
- `shell` / `task_runner` — Shell-Zugriff

## Backend-Scope (TODO)

Aktuell ergibt Login einen vollen Client-Token mit Zugriff auf alle Tools.  
Ziel: ai-coder soll als eigener `client_profile = ai_coder` laufen.  
Details: `docs/backend_scope.md`
