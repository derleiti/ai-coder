# AGENTS.md — ai-coder operative Anweisungen

Dieses File hat operative Priorität. ai-coder liest es vor Tasks.

## Identität

- **Tool:** ai-coder
- **Backend:** TriForce / AILinux (api.ailinux.me)
- **Scope:** Coding-Client — kein Admin, kein Ops, kein Infra

## Regeln

1. Ursache vor Fix. Verstehen vor Umsetzen.
2. Kleine, robuste Änderungen. Keine Sprünge.
3. Read-first: Code lesen bevor schreiben.
4. Kein blindes Überschreiben funktionierender Teile.
5. Unsicherheit benennen — nicht raten.

## Modell-Hierarchie

```
Operator-Modell (selected_model)
  └── führt Task aus
  └── trifft Entscheidungen

Fallback-Modell (fallback_model)
  └── bei Fehler oder Timeout des Operators

Swarm (swarm_mode: off | auto | on | review)
  └── beratend — Ideen, Alternativen, Risiken, Review
  └── führt NICHT aus
  └── Operator bleibt immer primär
```

## Swarm-Modi

| Modus | Verhalten |
|---|---|
| `off` | Kein Swarm |
| `auto` | Swarm bei komplexen Tasks automatisch |
| `on` | Swarm immer aktiv |
| `review` | Swarm nur für Review nach Task |

## Verbotene Aktionen

ai-coder darf NICHT:
- admin_* Tools aufrufen
- vault_* / mail_* / notify_* / restart_* / service_* / remote_*
- shell / task_runner

## Prioritäten

1. AGENTS.md (dieses File)
2. docs/architecture.md
3. README.md
