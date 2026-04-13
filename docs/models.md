# Modelle — ai-coder

## Operator-Modell

Das aktive Coding-Modell. Führt Tasks aus. Trifft Entscheidungen.

```bash
aicoder model                           # anzeigen
aicoder model anthropic/claude-sonnet-4 # setzen
```

Gespeichert in: `~/.config/ai-coder/state.json` → `selected_model`

## Fallback-Modell

Wird verwendet wenn das Operator-Modell nicht antwortet oder einen Fehler zurückgibt.

```bash
aicoder fallback                              # anzeigen
aicoder fallback gemini/gemini-2.0-flash      # setzen
```

Gespeichert in: `~/.config/ai-coder/state.json` → `fallback_model`

## Modell-Notation

Format: `provider/model-name`

Beispiele:
- `anthropic/claude-sonnet-4`
- `anthropic/claude-opus-4`
- `gemini/gemini-2.0-flash`
- `gemini/gemini-2.5-pro`
- `groq/llama-3.3-70b-versatile`
- `ollama/qwen2.5:14b`

Verfügbare Modelle: `aicoder handshake` oder `/v1/client/models`

## AGENTS.md

Lege im Projekt-Root eine `AGENTS.md` an. ai-coder liest sie vor Tasks.  
Inhalt: Regeln, Konventionen, Scope, Verbote für das Projekt.
