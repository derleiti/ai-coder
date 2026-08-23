# Backend-Scope — ai-coder

## Ziel

AICoder ist ein allgemeiner Operator für Coding, Entwicklung, DevOps, System- und Infrastrukturaufgaben. Das Profil `ai-coder` dient der Identifikation, Telemetrie und kompatiblen Tool-Auslieferung — nicht als zusätzliche Coding-only-Berechtigungsstufe.

## Autorisierung

Die tatsächliche Berechtigung kommt vom angemeldeten TriForce-Konto und dessen serverseitigem RBAC/Tier. Der Client darf keine Rechte erfinden oder einen Backend-Deny umgehen, soll aber auch keine vom Backend legitim angebotenen Operator-Tools pauschal nach Namen herausfiltern.

Das bedeutet:

- Backend-RBAC entscheidet, welche Fähigkeiten der Account grundsätzlich besitzt.
- Der Backend-Toolkatalog beschreibt, welche Fähigkeiten in dieser Session angeboten werden.
- AICoder wendet darauf lokale Risiko-, Workspace-, Approval- und PrivilegeBroker-Regeln an.
- `shell`, `binary_exec` und `task_runner` bleiben lokale AICoder-Fähigkeiten und werden nicht über MCP an einen Server umgeleitet.
- Elevation bleibt immer lokale Benutzerentscheidung und lokale Authentifizierung.

## Sicherheitsgrenzen

Die Sicherheitsgrenze ist nicht `coding vs. admin`, sondern die Wirkung einer Aktion:

- read-only Diagnose
- Mutation
- Workspace-Escape
- Elevation
- destruktive Aktion
- Security-Änderung
- sensible Daten / externe Kommunikation

Neue Backend-Fähigkeiten sollen anhand dieser Eigenschaften klassifiziert werden, nicht durch pauschale Namensverbote.
