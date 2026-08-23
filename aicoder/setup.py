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
    SWARM_MODES, APPROVAL_MODES, DEFAULT_RUNTIME_MODE, get_state,
    set_approval_mode, set_fallback, set_model, set_runtime_mode, set_swarm, set_tool_mode, set_workspace,
)
from .ui import C, bold, dim, cyan, green, yellow, red, magenta, white, panel, term_width, reset_live_line
from .workspace import active_workspace
from .repl_input import COMMANDS, PromptCancelled, ReplInput
from . import settings as settings_core



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


def _ensure_valid_session() -> bool:
    """Return whether the stored session can still be used.

    Re-authentication belongs to ``run_setup`` so there is only one login
    path for CLI, REPL and first-run setup.
    """
    try:
        session = load_session()
        if not _is_token_expired(session.token):
            return True
        print("  \033[33mSession abgelaufen — Login erforderlich\033[0m")
        return False
    except Exception:
        return False


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
    previous_session = None
    try:
        previous_session = load_session()
        if _is_token_expired(previous_session.token):
            logged_in = False
            print(f"\n{_c('yellow','! Session abgelaufen. Bitte erneut einloggen.')}")
        else:
            session = previous_session
            print(f"\n✓ Eingeloggt als {_c('green', session.user_id)}  "
                  f"(tier={session.tier}  base={session.base_url})")
            logged_in = True
    except RuntimeError:
        logged_in = False
        print(f"\n{_c('yellow','! Nicht eingeloggt.')}")

    if not logged_in:
        print("\n── Login ──────────────────────────────────")
        base = _ask(
            "Backend URL",
            previous_session.base_url if previous_session else DEFAULT_BASE_URL,
        )
        email = _ask(
            "E-Mail",
            previous_session.user_id if previous_session else "",
        )
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
        state = get_state()
        effective_fallback = state.get("fallback_model") or ""
        if effective_fallback:
            print(f"  fallback → {_c('green', effective_fallback)}")
        else:
            print(f"  fallback → {_c('dim', 'disabled (same as operator)')}")
    else:
        set_fallback("")
        print(f"  fallback → {_c('dim', 'disabled')}")

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
    ws_default = str(active_workspace(state.get("workspace_root")))
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

    def _write_history_safely() -> None:
        try:
            readline.write_history_file(str(histfile))
        except OSError:
            pass

    atexit.register(_write_history_safely)

    # Keybindings: Ctrl+J = literal newline wird zu " && " (Multiline-Hack)
    try:
        readline.parse_and_bind("set editing-mode emacs")
        readline.parse_and_bind("set show-all-if-ambiguous on")
        readline.parse_and_bind("set colored-completion-prefix on")
    except Exception:
        pass

    # Tab-Completion fuer Slash-Kommandos
    _commands = COMMANDS

    def _completer(text, state):
        if text.startswith("/"):
            matches = [c for c in _commands if c.startswith(text)]
        else:
            matches = []
        return matches[state] if state < len(matches) else None

    readline.set_completer(_completer)
    readline.parse_and_bind("tab: complete")


def _repl_settings_command(value: str) -> int:
    """Handle /settings through the same canonical registry/store as CLI and GUI."""
    parts = (value or "").split(None, 2)
    action = parts[0].lower() if parts else "list"
    try:
        if action in {"list", "ls"}:
            state = settings_core.STORE.load()
            for key in sorted(settings_core.REGISTRY, key=lambda k: (settings_core.REGISTRY[k].group, k)):
                spec = settings_core.REGISTRY[key]
                if spec.sensitive:
                    shown = "***"
                else:
                    shown = state.get(key, spec.default)
                    if key == "enabled_tools":
                        shown = "all" if shown is None else ("none" if shown == [] else ",".join(shown))
                print(f"  {key:<22} = {shown}")
            return 0
        if action == "get" and len(parts) >= 2:
            key = settings_core.resolve_key(parts[1])
            spec = settings_core.REGISTRY[key]
            shown = "***" if spec.sensitive else settings_core.STORE.get(key)
            print(f"  {key} = {shown}")
            return 0
        if action == "set" and len(parts) >= 3:
            key = settings_core.resolve_key(parts[1])
            spec = settings_core.REGISTRY[key]
            if not spec.mutable:
                raise settings_core.SettingsError(f"'{key}' is read-only.")
            value_to_set = settings_core.coerce(key, parts[2])
            saved = settings_core.STORE.set(key, value_to_set)
            shown = "***" if spec.sensitive else saved.get(key)
            print(f"  {key} → {shown}")
            return 0
        if action == "reset" and len(parts) >= 2:
            key = settings_core.resolve_key(parts[1])
            saved = settings_core.STORE.reset(key)
            spec = settings_core.REGISTRY[key]
            shown = "***" if spec.sensitive else saved.get(key)
            print(f"  {key} → {shown}")
            return 0
        if action in {"explain", "describe"} and len(parts) >= 2:
            data = settings_core.describe(parts[1])
            print(f"  {data['key']} [{data['type']}] · group={data['group']}")
            print(f"  {data['description']}")
            print(f"  current={data['value']} · default={data['default']}")
            if data["choices"]:
                print(f"  choices={','.join(data['choices'])}")
            if data["aliases"]:
                print(f"  aliases={','.join(data['aliases'])}")
            if data["security_impact"]:
                print("  security-impacting setting")
            return 0
    except settings_core.SettingsError as exc:
        print(f"  Fehler: {exc}")
        return 2

    print("  usage: /settings [list|get KEY|set KEY VALUE|reset KEY|explain KEY]")
    return 2


def run_repl(skip_setup: bool = False) -> int:
    """
    Interaktiver Agent-REPL.
    Startet Setup-Wizard wenn nötig, dann Agent-Loop.
    """
    _setup_readline()

    session_valid = _ensure_valid_session()
    if not skip_setup or not session_valid:
        ok = run_setup()
        if not ok:
            return 1

    state = get_state()
    model    = state.get("selected_model")
    fallback = state.get("fallback_model")
    swarm    = state.get("swarm_mode","off")
    ws       = str(active_workspace(state.get("workspace_root")))

    def _toolbar() -> str:
        current = get_state()
        active_model = current.get("selected_model") or "backend"
        mode = current.get("tool_mode", "on_demand")
        approval = current.get("approval_mode", "ask")
        runtime = current.get("runtime_mode", DEFAULT_RUNTIME_MODE)
        return f"  {active_model} · runtime:{runtime} · tools:{mode} · approvals:{approval} · swarm:{current.get('swarm_mode', 'off')}"

    repl_input = ReplInput(CONFIG_DIR / "history", _toolbar)
    conversation: list[dict] = []

    def _print_repl_header() -> None:
        nonlocal state, model, fallback, swarm, ws
        state = get_state()
        model = state.get("selected_model")
        fallback = state.get("fallback_model")
        swarm = state.get("swarm_mode", "off")
        ws = str(active_workspace(state.get("workspace_root")))
        tool_mode = state.get("tool_mode", "on_demand")
        enabled = state.get("enabled_tools")
        timeout = int(state.get("request_timeout", 300))
        try:
            session = load_session()
            identity = f"{session.user_id} · {session.tier}"
        except Exception:
            identity = "offline"

        w = max(48, min(term_width(), 92))
        rule = "─" * (w - 4)
        print()
        print(f"  {C.BOLD}{C.BCYAN}◆ ai-coder{C.RESET}  {C.DIM}interactive agent{C.RESET}")
        print(f"  {C.DIM}{rule}{C.RESET}")
        print(f"  {dim('account  ')} {cyan(identity)}")
        print(f"  {dim('operator ')} {cyan(model or '(backend default)')}")
        print(f"  {dim('fallback ')} {dim(fallback or '—')}")
        approval_mode = state.get("approval_mode", "ask")
        runtime_mode = state.get("runtime_mode", DEFAULT_RUNTIME_MODE)
        print(f"  {dim('runtime  ')} mode={cyan(runtime_mode)} · tools={cyan(tool_mode)} · enabled={cyan('all' if enabled is None else str(len(enabled)))} · "
              f"approvals={cyan(approval_mode)} · swarm={cyan(swarm)} · timeout={cyan(str(timeout)+'s')}")
        print(f"  {dim('workspace')} {dim(ws)}")
        print(f"  {C.DIM}{rule}{C.RESET}")
        if repl_input.enhanced:
            print(f"  {dim('Enter send · Alt+Enter newline · Ctrl+C clear/cancel · Ctrl+R history · Tab commands')}")
        else:
            print(f"  {yellow('Basic input mode')} {dim('· install prompt-toolkit for multiline editing and safe repaint')}")
        print(f"  {dim('/help · /command <name> [args] · /runtime native-light · /plan · /new · /exit')}")
        print(f"  {C.DIM}{rule}{C.RESET}")

    _print_repl_header()

    from .agent import run_agent

    while True:
        try:
            reset_live_line()
            prompt = repl_input.read(f"\n  {C.BOLD}{C.BCYAN}◆{C.RESET} ").strip()
        except PromptCancelled:
            print(f"  {dim('prompt cancelled')}")
            continue
        except KeyboardInterrupt:
            print(f"  {dim('prompt cancelled')}")
            continue
        except EOFError:
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
                _print_repl_header()
            elif cmd == "/model":
                if val:
                    set_model(val)
                    refreshed = get_state()
                    model = refreshed.get("selected_model")
                    fallback = refreshed.get("fallback_model")
                    print(f"  model → {val}")
                else:
                    new = model_picker_interactive(current_model=model or "")
                    if new and new != model:
                        set_model(new)
                        model = new
                        print(f"  model → {cyan(model)}")
            elif cmd == "/fallback" and val:
                set_fallback(val)
                fallback = get_state().get("fallback_model") or ""
                print(f"  fallback → {fallback or 'disabled'}")
            elif cmd == "/swarm" and val:
                try:
                    set_swarm(val)
                    swarm = val
                    print(f"  swarm → {val}")
                except ValueError as e:
                    print(f"  Fehler: {e}")
            elif cmd == "/status":
                _print_repl_header()
            elif cmd == "/tools":
                if val:
                    try:
                        set_tool_mode(val.strip())
                        print(f"  tools → {val.strip()}")
                        _print_repl_header()
                    except ValueError as e:
                        print(f"  Fehler: {e}")
                else:
                    print(f"  tools = {get_state().get('tool_mode', 'on_demand')}")
                    print("  set: /tools off|on_demand|always")
            elif cmd == "/settings":
                _repl_settings_command(val)
                state = get_state()
                model = state.get("selected_model")
                fallback = state.get("fallback_model")
                swarm = state.get("swarm_mode", "off")
            elif cmd == "/runtime":
                if val:
                    try:
                        set_runtime_mode(val.strip())
                        print(f"  runtime → {val.strip()}")
                        _print_repl_header()
                    except ValueError as e:
                        print(f"  Fehler: {e}")
                else:
                    print(f"  runtime = {get_state().get('runtime_mode', DEFAULT_RUNTIME_MODE)}")
            elif cmd == "/guidelines":
                from .guidelines import load_guidelines
                workspace = str(active_workspace(get_state().get("workspace_root")))
                rows = load_guidelines(workspace)
                if not rows:
                    print("  no guidelines discovered")
                for scope, text in rows:
                    print(f"\n  [{scope}]\n{text}")
            elif cmd == "/commands":
                from .commands import discover_commands
                workspace = str(active_workspace(get_state().get("workspace_root")))
                commands = discover_commands(workspace)
                if not commands:
                    print("  no commands discovered")
                for item in commands:
                    print(f"  {item.name:<20} {item.scope:<18} {item.description}")
            elif cmd == "/command":
                from .commands import expand_command
                command_parts = val.split(None, 1)
                if not command_parts:
                    print("  usage: /command <name> [arguments]")
                else:
                    command_name = command_parts[0]
                    command_args = command_parts[1] if len(command_parts) > 1 else ""
                    expanded, is_error = expand_command(
                        str(active_workspace(get_state().get("workspace_root"))),
                        command_name,
                        command_args,
                    )
                    if is_error:
                        print(f"  {expanded}")
                    else:
                        try:
                            run_agent(
                                initial_prompt=expanded,
                                model=model,
                                fallback_model=fallback,
                                conversation=conversation,
                                runtime_mode="native-light",
                            )
                        except KeyboardInterrupt:
                            print(f"\n{_c('yellow','[unterbrochen]')}")
                        except Exception as e:
                            print(f"\n[Fehler] {e}", file=sys.stderr)
            elif cmd == "/plan":
                from .agent_plan import PlanStore, format_plan
                workspace = str(active_workspace(get_state().get("workspace_root")))
                store = PlanStore()
                if val.strip().lower() == "clear":
                    print("  current plan cleared" if store.clear_current(workspace) else "  no current plan")
                elif val.strip().lower() == "list":
                    plans = store.list(workspace, limit=10)
                    if not plans:
                        print("  no plans")
                    for plan in plans:
                        print(f"  {plan.id}  {plan.status:<9} iter={plan.iteration:<3} {plan.task[:60]}")
                else:
                    plan = store.load_current(workspace)
                    if plan is None:
                        print("  no current plan")
                    else:
                        print("\n" + format_plan(plan))
            elif cmd == "/clear":
                if sys.stdout.isatty():
                    print("\033[2J\033[H", end="")
                _print_repl_header()
            elif cmd == "/new":
                conversation.clear()
                print(f"  {cyan('new session')} {dim('· conversation context cleared')}")
            elif cmd == "/keys":
                print("  Enter        Aufgabe senden")
                print("  Alt+Enter    Neue Zeile (Shift+Enter in kompatiblen Terminals)")
                print("  Ctrl+C       Eingabe leeren; leer erneut = aktuellen Prompt abbrechen")
                print("  Ctrl+D       Zeichen löschen; bei leerer Eingabe Session beenden")
                print("  Ctrl+R       History durchsuchen")
                print("  Ctrl+P/N     Vorige/nächste History")
                print("  Ctrl+L       Terminal neu zeichnen")
                print("  Tab          Slash-Kommandos vervollständigen")
            elif cmd == "/permissions":
                if val:
                    aliases = {"manual": "ask", "auto": "autopilot"}
                    requested = aliases.get(val.strip().lower(), val.strip().lower())
                    try:
                        set_approval_mode(requested)
                        print(f"  approvals → {requested}")
                    except ValueError as e:
                        print(f"  Fehler: {e}")
                else:
                    active = get_state().get("approval_mode", "ask")
                    print(f"  Lokale Berechtigungsrichtlinie · aktiv: {active}")
                    print("  ask        jede Änderung einzeln bestätigen")
                    print("  autopilot  normale Schreibzugriffe automatisch; sudo/delete weiter bestätigen")
                    print("  all        Workspace-Schreibzugriffe automatisch; Löschen weiter bestätigen")
                    print("  root/sudo  im Coding-only-Profil grundsätzlich deaktiviert")
                    print("  Setzen: /permissions ask|autopilot|all")
            elif cmd == "/shell":
                print("  /shell ist im Coding-only-Profil deaktiviert.")
            elif cmd == "/models":
                try:
                    from .client import TriForceClient
                    s = load_session()
                    c = TriForceClient(s.base_url, token=s.token, timeout=10)
                    data = c._request("GET", "/v1/client/models", require_auth=True, _label="models")
                    from .client import model_identifier
                    models = sorted(
                        model_id for item in data.get("models", [])
                        if (model_id := model_identifier(item))
                    )
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
                print("  /model <n> · /fallback <n> · /swarm <m> · /models · /status")
                print("  /runtime classic|native-light · /tools off|on_demand|always · /settings · /plan [list|clear]")
                print("  /commands · /command <name> [args] · /guidelines")
                print("  /setup · /new · /clear · /keys · /permissions · /exit")
            else:
                print(f"  Unbekannt: {cmd}  — /help für Hilfe")
            continue

        # Agent-Task ausführen
        try:
            run_agent(
                initial_prompt=prompt,
                model=model,
                fallback_model=fallback,
                conversation=conversation,
            )
        except KeyboardInterrupt:
            print(f"\n{_c('yellow','[unterbrochen]')}")
        except Exception as e:
            print(f"\n[Fehler] {e}", file=sys.stderr)

    return 0
