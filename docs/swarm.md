# Swarm — ai-coder

## Konzept

Der Swarm ist eine beratende Zusatzinstanz. Er führt NICHT aus.

```
Operator-Modell  →  führt Task aus
Swarm            →  Ideen / Alternativen / Risiken / Review
```

Der Operator bleibt immer primär. Der Swarm ist nie der Ausführer.

## Modi

```bash
aicoder swarm off     # kein Swarm (default)
aicoder swarm auto    # Swarm bei komplexen Tasks
aicoder swarm on      # Swarm immer aktiv
aicoder swarm review  # Swarm nur nach Task (Review)
```

## Aktueller Stand

- `on`: Operator und konfiguriertes Advisor-/Fallback-Modell können parallel
  befragt werden; der Operator bleibt das primäre Ergebnis.
- `review`: Der Operator läuft zuerst. Erst danach erhält das Advisor-Modell
  den tatsächlichen Operator-Output zur Prüfung.
- Ohne separates Fallback-Modell wird kein doppelter Backend-Default-Call erzeugt.
- Bei Task-Ausgaben ist der Swarm nur beratend und schreibt niemals Dateien.

## Bekannte Probleme (V1)

Echte Swarm-Calls via TriForce hatten in V1 Timeout-Probleme bei mehreren parallelen Requests.  
Nicht mit Gewalt in V2 erzwingen — erst Architektur klären.
