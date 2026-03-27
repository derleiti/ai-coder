# ai-coder

Terminalbasierter Coding & DevOps Agent für AILinux / TriForce.

**Prinzip:** Dünner lokaler CLI-Client — Intelligenz sitzt im Backend.

## Download

| Plattform | Download | Hinweis |
|---|---|---|
| **Windows** | [aicoder.exe](https://github.com/derleiti/ai-coder/releases/latest/download/aicoder.exe) | Standalone, 13 MB, PowerShell-ready |
| **Linux (Binary)** | [aicoder-x86_64-linux](https://github.com/derleiti/ai-coder/releases/latest/download/aicoder-x86_64-linux) | Standalone, ~8 MB |
| **Debian/Ubuntu** | [aicoder_amd64.deb](https://github.com/derleiti/ai-coder/releases/latest/download/aicoder_amd64.deb) | `sudo dpkg -i aicoder_*.deb` |
| **Arch / AILinux** | `yay -S aicoder` | AUR-Paket |
| **pip** | `pip install -e .` | Aus Quellcode |

### Windows-Installation

```powershell
# Option 1: Binary direkt nutzen
# aicoder.exe runterladen, in einen Ordner legen (z.B. C:\Tools)
# Ordner zu PATH hinzufügen (System → Umgebungsvariablen → Path → Bearbeiten)

# Option 2: NSIS-Installer
# aicoder-{version}-setup.exe runterladen und ausführen
# Installiert nach C:\Program Files\aicoder, fügt automatisch zu PATH hinzu
```

### Linux-Installation

```bash
# Binary direkt
sudo wget -O /usr/bin/aicoder https://github.com/derleiti/ai-coder/releases/latest/download/aicoder-x86_64-linux
sudo chmod +x /usr/bin/aicoder

# Oder über APT-Repo
echo "deb https://repo.ailinux.me stable main" | sudo tee /etc/apt/sources.list.d/ailinux.list
sudo apt update && sudo apt install aicoder
```

## Schnellstart

```bash
aicoder                        # Setup-Wizard + Agent-REPL
aicoder login                  # Login gegen TriForce Backend
aicoder model anthropic/claude-sonnet-4
aicoder fallback gemini/gemini-2.0-flash
aicoder swarm auto
aicoder status
```

## Commands

| Command | Beschreibung |
|---|---|
| `login` | Login gegen /v1/auth/login |
| `logout` | Lokale Session löschen |
| `whoami` | Token verifizieren |
| `model [value]` | Aktives Modell anzeigen / setzen |
| `fallback [value]` | Fallback-Modell anzeigen / setzen |
| `swarm [value]` | Swarm-Modus: off / auto / on / review |
| `status` | Übersicht: model, fallback, swarm, workspace, docs |
| `ask <prompt>` | Single-shot LLM mit AGENTS.md als System-Prompt |
| `chat` | Interaktive Multi-Turn-Session |
| `task <file>` | File lesen → LLM → Diff → optional apply |
| `review <file>` | Strukturiertes Code-Review |
| `agent <prompt>` | Autonomer Agent mit Tool-Loop |
| `models [--filter]` | Verfügbare Modelle vom Backend |
| `mcp <tool> [args]` | MCP-Tool-Call gegen /v1/mcp |
| `workspace [path]` | Lokalen Repo-Snapshot erstellen |
| `hist [-n N]` | Call-History anzeigen |

## Architektur

Siehe `docs/architecture.md`.

## CI/CD

- **Linux:** PyInstaller Build auf Push (GitHub Actions)
- **Windows:** PyInstaller Build + NSIS Installer auf Tag-Push (`v*`)
- Automatische GitHub Releases mit Binaries

## Links

- **Website:** https://ailinux.me/ai-coder/
- **Downloads:** https://ailinux.me/downloads/
- **API-Docs:** https://api.ailinux.me
- **Forum:** https://forum.ailinux.me
- **Beta-Code:** `AILINUX2026` (kostenloser Pro-Zugang)
