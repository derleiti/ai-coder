from __future__ import annotations
"""
setup.py — Setup-Wizard + Agent-REPL.

Wird gestartet wenn:
  - `aicoder` ohne Argumente aufgerufen wird
  - Kein Modell in state.json konfiguriert ist  (Setup-Mode)
  - Modell gesetzt → direkt Agent-REPL starten  (Agent-Mode)
"""

import json
import sys
from getpass import getpass
from pathlib import Path
from typing import Optional

from .config import DEFAULT_BASE_URL, Session, load_session, save_session
from .session_state import (
    SWARM_MODES, get_state,
    set_fallback, set_model, set_swarm, set_workspace,
)
from .ui import C, bold, dim, cyan, green, yellow, red, magenta, white, panel, term_width


# ── Interaktiver Model-Picker ────────────────────────────────────────────────
# Links/Rechts: Provider wechseln | Hoch/Runter: Modell wählen | Enter: OK | q: Abbruch

PROVIDER_ORDER = [
    "anthropic","gemini","mistral","groq","cerebras",
    "openrouter","cloudflare","github","ollama","other"
]

def _read_key() -> str:
    """Liest einen einzelnen Keypress. Unix: termios/tty. Windows: msvcrt."""
    import platform
    if platform.system() == "Windows":
        try:
            import msvcrt
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):  # Sondertaste (Pfeile etc.)
                ch2 = msvcrt.getwch()
                return {"H": "UP", "P": "DOWN", "M": "RIGHT", "K": "LEFT"}.get(ch2, "?")
            if ch == "\r":
                return "\n"
            if ch == "\x03":
                return "q"
            return ch
        except Exception:
            return input() or "\n"
    else:
        try:
            import termios, tty
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                ch = os.read(fd, 1)
                if ch == b"\x1b":
                    ch2 = os.read(fd, 1)
                    if ch2 == b"[":
                        ch3 = os.read(fd, 1)
                        return {b"A": "UP", b"B": "DOWN", b"C": "RIGHT", b"D": "LEFT"}.get(ch3, "?")
                    return "ESC"
                return ch.decode("utf-8", errors="replace")
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception:
            return input() or "\n"


def _group_models(models: list) -> dict:
    """Gruppiert Model-Liste nach Provider, sortiert nach PROVIDER_ORDER."""
    groups: dict = {}
    for m in models:
        p = m.get("provider", "other")
        # nur chat-fähige
        cats = m.get("categories", [])
        if m.get("media_image") or m.get("media_video"):
            continue
        (groups.setdefault(p, [])).append(m)
    # sortieren
    ordered = {}
    for p in PROVIDER_ORDER:
        if p in groups:
            ordered[p] = groups[p]
    for p in groups:
        if p not in ordered:
            ordered[p] = groups[p]
    return ordered


def model_picker_interactive(current_model: str = "") -> str:
    """
    Interaktiver TUI-Picker.
    ← → : Provider wechseln
    ↑ ↓ : Modell wählen
    Enter: bestätigen
    q/ESC: abbrechen (gibt current_model zurück)
    """
    from .config import load_session
    from .client import TriForceClient

    # Modelle laden
    try:
        session = load_session()
        client  = TriForceClient(session.base_url, session.token)
        all_models = client.list_models()
    except Exception:
        all_models = []

    # Fallback: hartcodierte Provider-Liste
    if not all_models:
        print("  (Keine Verbindung — manuelle Eingabe)")
        val = input(f"  Modell-ID [{current_model}]: ").strip()
        return val or current_model

    groups = _group_models(all_models)
    providers = list(groups.keys())
    if not providers:
        return current_model

    # Startposition: provider und modell des aktuellen Modells finden
    cur_prov_idx = 0
    cur_mod_idx  = 0
    for pi, p in enumerate(providers):
        for mi, m in enumerate(groups[p]):
            mid = m.get("id", m.get("model", ""))
            if mid == current_model:
                cur_prov_idx = pi
                cur_mod_idx  = mi
                break

    VISIBLE = 12  # sichtbare Modelle gleichzeitig

    def _render(prov_idx: int, mod_idx: int):
        os.system("cls" if __import__("platform").system() == "Windows" else "clear")
        w = min(os.get_terminal_size().columns, 100)
        prov = providers[prov_idx]
        mods = groups[prov]
        total_mods = len(mods)

        # Provider-Leiste
        prov_bar = ""
        for i, p in enumerate(providers):
            cnt = len(groups[p])
            if i == prov_idx:
                prov_bar += f"\033[1;36m[ {p} ({cnt}) ]\033[0m "
            else:
                prov_bar += f"\033[2m{p} ({cnt})\033[0m  "
        print(f"\n  {prov_bar}")
        print(f"  \033[2m{'─' * (w-4)}\033[0m")
        print(f"  \033[2m← → Provider  ↑ ↓ Modell  Enter=OK  q=Abbruch\033[0m")
        print(f"  \033[2m{'─' * (w-4)}\033[0m")

        # Scroll-Fenster
        start = max(0, mod_idx - VISIBLE // 2)
        end   = min(total_mods, start + VISIBLE)
        start = max(0, end - VISIBLE)

        for i in range(start, end):
            m = mods[i]
            mid  = m.get("id", m.get("model", ""))
            name = m.get("name", mid)
            caps = m.get("capabilities", [])
            cap_str = " ".join(f"\033[2m[{c}]\033[0m" for c in caps if c != "chat")
            if i == mod_idx:
                print(f"  \033[1;32m▶ {name:<55}\033[0m {cap_str}")
            else:
                print(f"    \033[2m{name:<55}\033[0m {cap_str}")

        if total_mods > VISIBLE:
            print(f"\n  \033[2m{mod_idx+1}/{total_mods} Modelle\033[0m")

        # Aktuell gewähltes
        cur = groups[prov][mod_idx]
        cur_id = cur.get("id", cur.get("model", ""))
        print(f"\n  \033[1mAuswahl:\033[0m \033[36m{cur_id}\033[0m")

    while True:
        _render(cur_prov_idx, cur_mod_idx)
        key = _read_key()

        prov = providers[cur_prov_idx]
        mods = groups[prov]

        if key == "RIGHT":
            cur_prov_idx = (cur_prov_idx + 1) % len(providers)
            cur_mod_idx  = 0
        elif key == "LEFT":
            cur_prov_idx = (cur_prov_idx - 1) % len(providers)
            cur_mod_idx  = 0
        elif key == "DOWN":
            cur_mod_idx = min(cur_mod_idx + 1, len(mods) - 1)
        elif key == "UP":
            cur_mod_idx = max(cur_mod_idx - 1, 0)
        elif key in ("\r", "\n", " "):
            # Bestätigen
            selected = mods[cur_mod_idx].get("id", mods[cur_mod_idx].get("model", ""))
            os.system("cls" if __import__("platform").system() == "Windows" else "clear")
            return selected
        elif key in ("q", "Q", "ESC", "\x03"):
            os.system("cls" if __import__("platform").system() == "Windows" else "clear")
            return current_model


def _c(code: str, text: str) -> str:
    """Compat-Wrapper — nutzt ui.py."""
    m = {"bold": C.BOLD, "dim": C.DIM, "green": C.BGREEN,
         "yellow": C.BYELLOW, "cyan": C.CYAN, "reset": C.RESET,
         "red": C.BRED, "blue": C.BBLUE, "white": C.BWHITE}
    return m.get(code, "") + text + C.RESET

def _ask(prompt: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    try:
        val = input(f"{prompt}{hint}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return val or default

def _pick(prompt: str, options: list[str], default: str = "") -> str:
    print(f"\n{prompt}")
    for i, o in enumerate(options, 1):
        marker = " ◀" if o == default else ""
        print(f"  {i}) {o}{marker}")
    while True:
        try:
            val = input(f"  Wahl [1-{len(options)}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return default
        if not val and default:
            return default
        if val.isdigit() and 1 <= int(val) <= len(options):
            return options[int(val)-1]
        # Direkte Eingabe auch erlaubt
        if val:
            return val


# ── Setup-Wizard ─────────────────────────────────────────────────────────────

def run_setup(force: bool = False) -> bool:
    """
    Setup-Wizard. Gibt True zurück wenn Setup erfolgreich/vollständig.
    """
    state = get_state()
    needs_setup = force or not state.get("selected_model")

    print(_c("bold", "\n╔══════════════════════════════════════════╗"))
    print(_c("bold",   "║        ai-coder  —  AILinux Agent        ║"))
    print(_c("bold",   "╚══════════════════════════════════════════╝"))

    # Session prüfen
    try:
        session = load_session()
        print(f"\n✓ Eingeloggt als {_c('green', session.user_id)}  "
              f"(tier={session.tier}  base={session.base_url})")
        logged_in = True
    except RuntimeError:
        logged_in = False
        print(f"\n{_c('yellow','! Nicht eingeloggt.')}")

    if not logged_in:
        print("\n── Login ──────────────────────────────────")
        base = _ask("Backend URL", DEFAULT_BASE_URL)
        email = _ask("E-Mail")
        password = getpass("Passwort: ")
        if email and password:
            from .client import ClientError, TriForceClient
            client = TriForceClient(base)
            try:
                result = client.login(email=email, password=password)
                session = Session(
                    base_url=base, token=result["token"],
                    client_id=result.get("client_id",""),
                    user_id=result.get("user_id", email),
                    tier=result.get("tier","unknown"),
                    account_role=result.get("account_role","unknown"),
                )
                save_session(session)
                print(f"✓ Login OK: {_c('green', session.user_id)}")
                logged_in = True
            except (ClientError, Exception) as e:
                print(f"✗ Login fehlgeschlagen: {e}", file=sys.stderr)
                return False
        else:
            print("Abgebrochen.")
            return False

    if not needs_setup:
        return True

    print("\n── Modell-Konfiguration ───────────────────")

    # Verfügbare Modelle laden
    popular = [
        "groq/llama-3.3-70b-versatile",
        "groq/moonshotai/kimi-k2-instruct",
        "groq/qwen/qwen3-32b",
        "gemini/gemini-2.0-flash",
        "gemini/gemini-2.5-pro",
        "anthropic/claude-sonnet-4",
        "ollama/qwen3:8b",
        "mistral/mistral-large-latest",
        "(andere eingeben)",
    ]
    print(_c("dim", "  Tip: aicoder models --group  zeigt alle 659 Modelle"))
    print(_c("dim", "  Öffne interaktiven Modell-Picker..."))
    model = model_picker_interactive(current_model=state.get("selected_model") or "")
    if not model:
        model = _pick("Operator-Modell wählen:", popular,
                      default=state.get("selected_model") or "groq/llama-3.3-70b-versatile")
    if model == "(andere eingeben)":
        model = _ask("Modell-ID eingeben", "groq/llama-3.3-70b-versatile")
    set_model(model)
    print(f"  model → {_c('green', model)}")

    print("\n── Fallback-Modell ────────────────────────")
    fallback_opts = [
        "groq/moonshotai/kimi-k2-instruct",
        "groq/llama-3.3-70b-versatile",
        "groq/qwen/qwen3-32b",
        "ollama/deepseek-v3.2:cloud",
        "(keins)",
        "(andere eingeben)",
    ]
    fallback = _pick("Fallback bei Fehler/Timeout:", fallback_opts,
                     default=state.get("fallback_model") or "groq/moonshotai/kimi-k2-instruct")
    if fallback == "(andere eingeben)":
        fallback = _ask("Fallback-ID", "")
    if fallback and fallback != "(keins)":
        set_fallback(fallback)
        print(f"  fallback → {_c('green', fallback)}")

    print("\n── Swarm-Modus ────────────────────────────")
    swarm_descs = {
        "off":    "Kein Swarm — nur Operator-Modell",
        "auto":   "Auto — Swarm bei komplexen Prompts (>150 Zeichen oder Keywords)",
        "on":     "Immer — Operator + Fallback parallel",
        "review": "Review — Fallback bewertet Operator-Output nach Task",
    }
    swarm_opts = [f"{k}  ({v})" for k,v in swarm_descs.items()]
    swarm_choice = _pick("Swarm-Modus:", swarm_opts,
                          default=f"{state.get('swarm_mode','auto')}  ({swarm_descs.get(state.get('swarm_mode','auto'),'')})")
    swarm = swarm_choice.split()[0].strip()
    if swarm in SWARM_MODES:
        set_swarm(swarm)
        print(f"  swarm → {_c('green', swarm)}")

    print("\n── Workspace ──────────────────────────────")
    ws_default = state.get("workspace_root") or str(Path.cwd())
    workspace = _ask("Projekt-Verzeichnis", ws_default)
    if workspace and Path(workspace).exists():
        set_workspace(workspace)
        print(f"  workspace → {_c('green', workspace)}")

    print(f"\n{_c('green', '✓ Setup abgeschlossen.')}")
    return True


# ── Agent-REPL ────────────────────────────────────────────────────────────────

def run_repl(skip_setup: bool = False) -> int:
    """
    Interaktiver Agent-REPL.
    Startet Setup-Wizard wenn nötig, dann Agent-Loop.
    """
    if not skip_setup:
        ok = run_setup()
        if not ok:
            return 1

    state = get_state()
    model    = state.get("selected_model")
    fallback = state.get("fallback_model")
    swarm    = state.get("swarm_mode","off")
    ws       = state.get("workspace_root") or str(Path.cwd())

    w = min(term_width(), 80)
    print()
    print(f"  {C.BOLD}{C.BCYAN}◆ ai-coder{C.RESET}  {C.DIM}Agent REPL{C.RESET}")
    print(f"  {C.DIM}{'─' * (w-4)}{C.RESET}")
    print(f"  {dim('model    ')} {cyan(model or '(backend default)')}")
    print(f"  {dim('fallback ')} {dim(fallback or '—')}")
    print(f"  {dim('swarm    ')} {dim(swarm)}")
    print(f"  {dim('workspace')} {dim(ws)}")
    print(f"  {C.DIM}{'─' * (w-4)}{C.RESET}")
    print(f"  {dim('/model <n>  /fallback <n>  /swarm <m>  /setup  /shell <cmd>  /exit')}")
    print(f"  {dim('Aufgabe eingeben → Agent führt sie autonom durch')}")
    print(f"  {C.DIM}{'─' * (w-4)}{C.RESET}")

    from .agent import run_agent

    while True:
        try:
            prompt = input(f"\n  {C.BOLD}{C.BCYAN}◆{C.RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{_c('dim','Session beendet.')}")
            break

        if not prompt:
            continue

        # Slash-Kommandos
        if prompt.startswith("/"):
            parts = prompt.split(None, 1)
            cmd   = parts[0].lower()
            val   = parts[1] if len(parts) > 1 else ""

            if cmd in ("/exit","/quit","/q"):
                print(_c("dim","Session beendet."))
                break
            elif cmd == "/setup":
                run_setup(force=True)
                state    = get_state()
                model    = state.get("selected_model")
                fallback = state.get("fallback_model")
                swarm    = state.get("swarm_mode","off")
            elif cmd == "/model":
                if val:
                    set_model(val)
                    model = val
                    print(f"  model → {val}")
                else:
                    new_model = model_picker_interactive(current_model=model or "")
                    if new_model and new_model != model:
                        set_model(new_model)
                        model = new_model
                        print(f"  model → {cyan(model)}")
            elif cmd == "/fallback" and val:
                set_fallback(val)
                fallback = val
                print(f"  fallback → {val}")
            elif cmd == "/swarm" and val:
                try:
                    set_swarm(val)
                    swarm = val
                    print(f"  swarm → {val}")
                except ValueError as e:
                    print(f"  Fehler: {e}")
            elif cmd == "/status":
                print(f"  model={model}  fallback={fallback}  swarm={swarm}")
            elif cmd == "/shell":
                if val:
                    import subprocess
                    r = subprocess.run(val, shell=True, capture_output=True, text=True)
                    if r.stdout: print(r.stdout.rstrip())
                    if r.stderr: print(r.stderr.rstrip(), file=sys.stderr)
                else:
                    print("  Bsp: /shell uptime")
            elif cmd == "/help":
                print("  /model <n>  /fallback <n>  /swarm <m>  /status  /shell <cmd>  /setup  /exit")
            else:
                print(f"  Unbekannt: {cmd}  — /help für Hilfe")
            continue

        # Agent-Task ausführen
        try:
            run_agent(
                initial_prompt=prompt,
                model=model,
                fallback_model=fallback,
            )
        except KeyboardInterrupt:
            print(f"\n{_c('yellow','[unterbrochen]')}")
        except Exception as e:
            print(f"\n[Fehler] {e}", file=sys.stderr)

    return 0
