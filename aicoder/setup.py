from __future__ import annotations
"""
setup.py — Setup-Wizard + Agent-REPL.

Wird gestartet wenn:
  - `aicoder` ohne Argumente aufgerufen wird
  - Kein Modell in state.json konfiguriert ist  (Setup-Mode)
  - Modell gesetzt → direkt Agent-REPL starten  (Agent-Mode)
"""

import json
import os
import sys
from getpass import getpass
from pathlib import Path
from typing import Optional

from .config import CONFIG_DIR, DEFAULT_BASE_URL, Session, load_session, save_session
from .session_state import (
    SWARM_MODES, get_state,
    set_fallback, set_model, set_swarm, set_workspace,
)
from .ui import C, bold, dim, cyan, green, yellow, red, magenta, white, panel, term_width



def _is_token_expired(token: str) -> bool:
    """Check JWT expiry using correct urlsafe base64 padding."""
    try:
        from .client import _decode_jwt_exp
        exp = _decode_jwt_exp(token)
        if exp is None: return False
        import time
        return exp < time.time()
    except Exception:
        return False


def _ensure_valid_session() -> None:
    """Re-Login wenn Token abgelaufen."""
    try:
        from .config import load_session, save_session, Session
        from .client import TriForceClient
        session = load_session()
        if not _is_token_expired(session.token):
            return
        print(f"  \033[33mToken abgelaufen — Re-Login erforderlich\033[0m")
        from getpass import getpass
        password = getpass(f"  Passwort für {session.user_id}: ")
        client = TriForceClient(session.base_url)
        result = client.login(email=session.user_id, password=password)
        new_session = Session(
            base_url=session.base_url,
            token=result["token"],
            client_id=result.get("client_id", session.client_id),
            user_id=result.get("user_id", session.user_id),
            tier=result.get("tier", session.tier),
            account_role=result.get("account_role", session.account_role),
        )
        save_session(new_session)
        print(f"  \033[32m✓ Re-Login OK\033[0m")
    except KeyboardInterrupt:
        print("\n  Re-Login abgebrochen.")
    except Exception as e:
        print(f"  \033[31m✗ Re-Login fehlgeschlagen: {e}\033[0m", file=__import__("sys").stderr)


# ── Interaktiver Model-Picker ──────────────────────────────────────────────
PROVIDER_ORDER = ["anthropic","gemini","mistral","groq","cerebras",
                  "openrouter","cloudflare","github","ollama","other"]

def _read_key() -> str:
    import platform
    if platform.system() == "Windows":
        try:
            import msvcrt
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                ch2 = msvcrt.getwch()
                return {"H":"UP","P":"DOWN","M":"RIGHT","K":"LEFT"}.get(ch2, "?")
            return "\n" if ch == "\r" else ("q" if ch == "\x03" else ch)
        except Exception:
            return input() or "\n"
    else:
        try:
            import termios, tty
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                ch = sys.stdin.buffer.read(1)
                if ch == b"\x1b":
                    ch2 = sys.stdin.buffer.read(1)
                    if ch2 == b"[":
                        ch3 = sys.stdin.buffer.read(1)
                        return {b"A":"UP",b"B":"DOWN",b"C":"RIGHT",b"D":"LEFT"}.get(ch3,"?")
                    return "ESC"
                return ch.decode("utf-8", errors="replace")
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception:
            return input() or "\n"


def _group_models(models: list) -> dict:
    groups: dict = {}
    for m in models:
        if m.get("media_image") or m.get("media_video"):
            continue
        p = m.get("provider", "other")
        groups.setdefault(p, []).append(m)
    ordered = {}
    for p in PROVIDER_ORDER:
        if p in groups:
            ordered[p] = groups[p]
    for p in groups:
        if p not in ordered:
            ordered[p] = groups[p]
    return ordered


def model_picker_interactive(current_model: str = "") -> str:
    """TUI Model-Picker: ←→ Provider, ↑↓ Modell, Enter=OK, q=Abbruch."""
    try:
        from .config import load_session
        from .client import TriForceClient
        session = load_session()
        all_models = TriForceClient(session.base_url, session.token).list_models()
    except Exception:
        all_models = []

    if not all_models:
        val = input(f"  Modell-ID [{current_model}]: ").strip()
        return val or current_model

    groups = _group_models(all_models)
    providers = list(groups.keys())
    if not providers:
        return current_model

    cur_prov, cur_mod = 0, 0
    for pi, p in enumerate(providers):
        for mi, m in enumerate(groups[p]):
            if m.get("id", m.get("model", "")) == current_model:
                cur_prov, cur_mod = pi, mi

    VISIBLE = 12

    def _cls():
        os.system("cls" if os.name == "nt" else "clear")

    def _render(pi, mi):
        _cls()
        mods = groups[providers[pi]]
        bar = ""
        for i, p in enumerate(providers):
            cnt = len(groups[p])
            bar += (f"\033[1;36m[ {p} ({cnt}) ]\033[0m " if i == pi
                    else f"\033[2m{p} ({cnt})\033[0m  ")
        print(f"\n  {bar}")
        try:
            w = min(os.get_terminal_size().columns - 4, 96)
        except Exception:
            w = 76
        print(f"  \033[2m{'─'*w}\033[0m")
        print(f"  \033[2m← → Provider  ↑ ↓ Modell  Enter=OK  q=Abbruch\033[0m")
        print(f"  \033[2m{'─'*w}\033[0m")
        total = len(mods)
        start = max(0, min(mi - VISIBLE//2, total - VISIBLE))
        for i in range(start, min(start + VISIBLE, total)):
            m = mods[i]
            mid = m.get("id", m.get("model", ""))
            name = m.get("name", mid)
            caps = " ".join(f"\033[2m[{c}]\033[0m" for c in m.get("capabilities",[]) if c != "chat")
            if i == mi:
                print(f"  \033[1;32m▶ {name:<55}\033[0m {caps}")
            else:
                print(f"    \033[2m{name:<55}\033[0m {caps}")
        if total > VISIBLE:
            print(f"\n  \033[2m{mi+1}/{total}\033[0m")
        cur_id = mods[mi].get("id", mods[mi].get("model", ""))
        print(f"\n  \033[1mAuswahl:\033[0m \033[36m{cur_id}\033[0m")

    while True:
        _render(cur_prov, cur_mod)
        key = _read_key()
        mods = groups[providers[cur_prov]]
        if key == "RIGHT":
            cur_prov = (cur_prov + 1) % len(providers); cur_mod = 0
        elif key == "LEFT":
            cur_prov = (cur_prov - 1) % len(providers); cur_mod = 0
        elif key == "DOWN":
            cur_mod = min(cur_mod + 1, len(mods) - 1)
        elif key == "UP":
            cur_mod = max(cur_mod - 1, 0)
        elif key in ("\r", "\n", " "):
            sel = mods[cur_mod].get("id", mods[cur_mod].get("model", ""))
            _cls()
            return sel
        elif key in ("q", "Q", "ESC", "\x03"):
            _cls()
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
    print(_c("dim", "  Tip: aicoder models --group  zeigt alle 625+ Modelle"))
    print(_c("dim", "  Öffne interaktiven Modell-Picker..."))
    _picked = model_picker_interactive(current_model=state.get("selected_model") or "")
    if _picked:
        model = _picked
    else:
        model = "groq/llama-3.3-70b-versatile"  # fallback

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
    if workspace:
        Path(workspace).mkdir(parents=True, exist_ok=True)
        set_workspace(workspace)
        print(f"  workspace → {_c('green', workspace)}")

    print(f"\n{_c('green', '✓ Setup abgeschlossen.')}")
    return True


# ── Agent-REPL ────────────────────────────────────────────────────────────────

def _setup_readline():
    """Readline konfigurieren: History, Cursor, Tab-Completion."""
    try:
        import readline
    except ImportError:
        return  # Windows ohne pyreadline — input() funktioniert trotzdem

    histfile = CONFIG_DIR / "history"
    histfile.parent.mkdir(parents=True, exist_ok=True)

    readline.set_history_length(500)
    try:
        readline.read_history_file(str(histfile))
    except (FileNotFoundError, OSError):
        pass

    import atexit
    atexit.register(readline.write_history_file, str(histfile))

    # Keybindings: Ctrl+J = literal newline wird zu " && " (Multiline-Hack)
    try:
        readline.parse_and_bind("set editing-mode emacs")
        readline.parse_and_bind("set show-all-if-ambiguous on")
        readline.parse_and_bind("set colored-completion-prefix on")
    except Exception:
        pass

    # Tab-Completion fuer Slash-Kommandos
    _commands = ["/model", "/fallback", "/swarm", "/status", "/shell",
                 "/setup", "/exit", "/quit", "/help", "/models"]

    def _completer(text, state):
        if text.startswith("/"):
            matches = [c for c in _commands if c.startswith(text)]
        else:
            matches = []
        return matches[state] if state < len(matches) else None

    readline.set_completer(_completer)
    readline.parse_and_bind("tab: complete")


def run_repl(skip_setup: bool = False) -> int:
    _ensure_valid_session()
    """
    Interaktiver Agent-REPL.
    Startet Setup-Wizard wenn nötig, dann Agent-Loop.
    """
    _setup_readline()

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
    print(f"  {dim('/model <n>  /fallback <n>  /swarm <m>  /models  /shell <cmd>  /exit')}")
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
                    new = model_picker_interactive(current_model=model or "")
                    if new and new != model:
                        set_model(new)
                        model = new
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
                    import subprocess, time as _t
                    _t0 = _t.time()
                    r = subprocess.run(val, shell=True, capture_output=True, text=True, timeout=60)
                    _dur = _t.time() - _t0
                    out = (r.stdout or "") + (r.stderr or "")
                    if r.stdout: print(r.stdout.rstrip())
                    if r.stderr: print(r.stderr.rstrip(), file=sys.stderr)
                    try:
                        from . import audit
                        audit.log_tool(tool_name="repl_shell", arguments={"command": val}, result=out[:2000], duration_s=_dur, is_error=r.returncode != 0, model="user/repl")
                    except Exception: pass
                else:
                    print("  Bsp: /shell uptime")
            elif cmd == "/models":
                try:
                    from .client import TriForceClient
                    from .config import load_session
                    s = load_session()
                    c = TriForceClient(s.base_url, token=s.token, timeout=10)
                    data = c._request("GET", "/v1/client/models", require_auth=True, _label="models")
                    models = sorted(data.get("models", []))
                    tier = data.get("tier", "?")
                    groups: dict = {}
                    for m in models:
                        p = m.split("/")[0] if "/" in m else "other"
                        groups.setdefault(p, []).append(m)
                    print(f"  {tier} — {len(models)} Modelle, {len(groups)} Provider")
                    for provider, mlist in sorted(groups.items()):
                        print(f"    [{provider}] {len(mlist)}: {', '.join(mlist[:3])}{'...' if len(mlist) > 3 else ''}")
                except Exception as e:
                    print(f"  Fehler: {e}")
            elif cmd == "/help":
                print("  /model <n>  /fallback <n>  /swarm <m>  /models  /status  /shell <cmd>  /setup  /exit")
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
