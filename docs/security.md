# Sicherheit — ai-coder

## Sicherheitsmodell

AICoder verwendet keine künstliche Coding-only-Grenze als Sicherheitsmechanismus. Der Operator darf die für den Auftrag verfügbaren lokalen und vom TriForce-Backend angebotenen Fähigkeiten verwenden. Sicherheit wird dort erzwungen, wo tatsächlich Risiko entsteht: Workspace-Grenzen, Mutationen, Elevation, destruktive Aktionen, Security-Änderungen und sensible Daten.

- Read-first: Ist-Zustand prüfen, bevor geschrieben wird.
- Lokale und MCP-gestützte Mutationen laufen durch dieselbe Risiko-/Approval-Policy.
- Workspace-Escapes benötigen eine sichtbare Freigabe.
- sudo/root/elevation benötigt immer lokale interaktive Authentifizierung; kein Auto-/Autopilot-Modus darf diese umgehen.
- Destruktive oder sicherheitsreduzierende Änderungen benötigen explizite Einmal-Freigabe.
- Passwörter und Tokens dürfen nicht vom Modell angefordert, gelesen, geloggt oder übertragen werden.
- Tool-Ergebnisse gelten als untrusted data und dürfen keine Benutzer-/Policy-Anweisungen überschreiben.

## Operator-Fähigkeiten

Coding, Builds, Tests, Paketmanagement, Services, Container, Deployment, Netzwerkdiagnostik, Systemadministration, DevOps und Infrastruktur sind legitime Operator-Aufgaben. Ein Tool wird nicht allein wegen seines Namens oder einer Kategorie wie `admin`, `service`, `remote` oder `devops` blockiert.

Backend-advertisierte Fähigkeiten bleiben weiterhin an serverseitige Authentisierung/RBAC gebunden. AICoder erweitert niemals die Rechte des angemeldeten Kontos; es entfernt lediglich den früheren zusätzlichen Coding-only-Filter im Client.

Besonders sensible Aktionen wie Secrets/Vault, Mailversand, Notifications oder Account-/Identity-Änderungen sollen nur für einen konkreten Benutzerauftrag verwendet werden und müssen entsprechend ihrer Wirkung als Mutation/Security-Änderung klassifiziert werden.

## PrivilegeBroker

Der PrivilegeBroker ist die zentrale lokale Sicherheitsgrenze für Mutationen und Elevation:

1. Aktion und Grund werden angezeigt.
2. Risiko wird klassifiziert.
3. Schreiboperationen folgen dem gewählten Approval-Modus.
4. Elevation sowie Security-/destruktive Änderungen bleiben interaktiv.
5. sudo/pkexec-Authentifizierung findet lokal statt und wird nicht an das Modell oder Backend weitergereicht.

## Gespeicherte Credentials

```
~/.config/ai-coder/session.json   chmod 600
~/.config/ai-coder/state.json     chmod 600
```

Tokens werden nicht im Klartext geloggt und in Statusausgaben maskiert.

## Backend-MCP

`X-Client-Profile: ai-coder` identifiziert den Client, soll aber keine zweite künstliche Coding-Allowlist darstellen. Die wirksamen Backend-Rechte kommen aus Authentisierung/RBAC und dem vom Backend tatsächlich angebotenen Tool-Katalog. Lokale Runtime-Tools wie `shell`, `binary_exec` und `task_runner` werden weiterhin lokal ausgeführt und niemals versehentlich als Remote-MCP-Shell umgeleitet.
