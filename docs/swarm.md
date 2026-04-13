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

## V2-Stand

Swarm-Modus wird in `state.json` gespeichert und bei `mcp`-Calls als Spinner-Label genutzt.  
Echte parallele Swarm-Calls ans Backend: geplant für V3.

## Bekannte Probleme (V1)

Echte Swarm-Calls via TriForce hatten in V1 Timeout-Probleme bei mehreren parallelen Requests.  
Nicht mit Gewalt in V2 erzwingen — erst Architektur klären.
