from __future__ import annotations
import argparse, json, sys, textwrap, time
from getpass import getpass
from typing import Any, Dict
from .client import ClientError, TriForceClient
from .config import DEFAULT_BASE_URL, Session, delete_session, load_session, save_session
from .docs_context import context_summary, read_agents_md
from .history import record as history_record, get_history, clear_history
from .session_state import (
    SWARM_MODES, get_state,
    set_fallback, set_model, set_swarm, set_workspace,
)
from .status import Spinner, phase_label
from .workspace import workspace_snapshot


def parse_kv_pairs(pairs: list[str]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for item in pairs:
        if "=" not in item:
            raise ClientError(f"Ungültiges Argument '{item}'. Erwartet key=value")
        key, value = item.split("=", 1)
        key = key.strip()
        value = value.strip()
        try:
            result[key] = json.loads(value)
        except Exception:
            result[key] = value
    return result


def session_client() -> tuple[Session, TriForceClient]:
    session = load_session()
    return session, TriForceClient(session.base_url, token=session.token)


def print_json(data: Dict[str, Any]) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


# ── Auth ────────────────────────────────────────────────────────────────────

def cmd_login(args: argparse.Namespace) -> int:
    email = args.email or input("E-Mail: ").strip()
    password = args.password or getpass("Passwort: ")
    client = TriForceClient(args.base_url)
    result = client.login(email=email, password=password)
    session = Session(
        base_url=args.base_url,
        token=result["token"],
        client_id=result.get("client_id", ""),
        user_id=result.get("user_id", email),
        tier=result.get("tier", "unknown"),
        account_role=result.get("account_role", "unknown"),
    )
    save_session(session)
    print(f"Login ok: {session.user_id} | tier={session.tier} | role={session.account_role}")
    print(f"client_id={session.client_id}")
    return 0


def cmd_logout(_: argparse.Namespace) -> int:
    delete_session()
    print("Session gelöscht.")
    return 0


def cmd_whoami(_: argparse.Namespace) -> int:
    _, client = session_client()
    print_json(client.verify())
    return 0


def cmd_handshake(_: argparse.Namespace) -> int:
    _, client = session_client()
    print_json(client.handshake())
    return 0


def cmd_tools(_: argparse.Namespace) -> int:
    _, client = session_client()
    data = client.handshake()
    tools = data.get("tools", [])
    print(f"{len(tools)} Tools erlaubt")
    for t in tools:
        print(t)
    return 0


def cmd_profile(_: argparse.Namespace) -> int:
    session = load_session()
    print_json(session.masked())
    return 0


def cmd_workspace(args: argparse.Namespace) -> int:
    snap = workspace_snapshot(args.path)
    # persist workspace root in state if it's a real git repo
    if snap.get("git_root"):
        set_workspace(snap["git_root"])
    print_json(snap)
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    _, client = session_client()
    arguments = parse_kv_pairs(args.arg or [])
    state = get_state()
    # Show active context before running
    swarm = state.get('swarm_mode', 'off')
    _print_header(state)
    label = phase_label(args.mode or swarm)
    with Spinner(label):
        data = client.mcp_call(args.tool, arguments)
    print_json(data)
    return 0


def cmd_status_demo(args: argparse.Namespace) -> int:
    label = phase_label(args.mode)
    with Spinner(label):
        time.sleep(args.seconds)
    print(f"{label} done")
    return 0


# ── Session State ────────────────────────────────────────────────────────────

def cmd_model(args: argparse.Namespace) -> int:
    if args.value:
        set_model(args.value)
        print(f"model → {args.value}")
    else:
        state = get_state()
        val = state.get("selected_model") or "(not set)"
        print(f"model = {val}")
    return 0


def cmd_fallback(args: argparse.Namespace) -> int:
    if args.value:
        set_fallback(args.value)
        print(f"fallback → {args.value}")
    else:
        state = get_state()
        val = state.get("fallback_model") or "(not set)"
        print(f"fallback = {val}")
    return 0


def cmd_swarm(args: argparse.Namespace) -> int:
    if args.value:
        try:
            set_swarm(args.value)
            print(f"swarm → {args.value}")
        except ValueError as e:
            print(f"Fehler: {e}", file=sys.stderr)
            return 1
    else:
        state = get_state()
        print(f"swarm = {state.get('swarm_mode', 'off')}")
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    state = get_state()
    ctx = context_summary(state.get("workspace_root"))

    model = state.get("selected_model") or "(not set)"
    fallback = state.get("fallback_model") or "(not set)"
    swarm = state.get("swarm_mode", "off")
    workspace = state.get("workspace_root") or ctx.get("project_root") or "(not set)"

    print("── ai-coder status ──────────────────────────────")
    print(f"  model    : {model}")
    print(f"  fallback : {fallback}")
    print(f"  swarm    : {swarm}")
    print(f"  workspace: {workspace}")
    print(f"  docs     : {ctx['doc_files_found']} file(s) found")
    if ctx.get("agents_md_present"):
        print("  AGENTS.md: ✓ present")
    else:
        print("  AGENTS.md: ✗ missing  ← create it for best results")
    if ctx["docs"]:
        for rel in ctx["docs"]:
            print(f"    · {rel}")
    print("─────────────────────────────────────────────────")
    return 0



# ── Ask / Chat ───────────────────────────────────────────────────────────────


def _print_header(state: dict, model_override: str | None = None) -> None:
    """Print active model/fallback/swarm before any LLM task."""
    model = model_override or state.get("selected_model") or "(backend default)"
    fallback = state.get("fallback_model") or "(not set)"
    swarm = state.get("swarm_mode", "off")
    print(f"model={model}  fallback={fallback}  swarm={swarm}", file=sys.stderr)

def _resolve_model(state: dict, override: str | None) -> str | None:
    """Return model to use: CLI arg > state selected_model > None (backend default)."""
    return override or state.get("selected_model") or None


def _print_response(result: dict) -> None:
    """Pretty-print chat response."""
    resp = result.get("response", "")
    model_used = result.get("model", "?")
    backend = result.get("backend", "?")
    latency = result.get("latency_ms")
    fallback = result.get("fallback_used", False)

    print()
    print(resp)
    print()
    meta = f"[{model_used} · {backend}"
    if latency:
        meta += f" · {latency}ms"
    if fallback:
        meta += " · FALLBACK"
    meta += "]"
    print(meta, file=sys.stderr)


def cmd_ask(args: argparse.Namespace) -> int:
    """Single-shot prompt. Reads AGENTS.md as system_prompt if present."""
    _, client = session_client()
    state = get_state()
    model = _resolve_model(state, getattr(args, "model", None))
    swarm = state.get("swarm_mode", "off")

    # Collect prompt: args.prompt (joined) or stdin
    if args.prompt:
        message = " ".join(args.prompt)
    else:
        print("Prompt (Enter + Ctrl-D to send):", file=sys.stderr)
        lines = []
        try:
            while True:
                lines.append(input())
        except EOFError:
            pass
        message = "\n".join(lines).strip()

    if not message:
        print("Fehler: kein Prompt angegeben.", file=sys.stderr)
        return 1

    # System prompt: AGENTS.md from workspace
    workspace = state.get("workspace_root")
    system_prompt = None
    if not getattr(args, "no_agents", False):
        system_prompt = read_agents_md(workspace)

    _print_header(state, model)

    # Swarm V2: on|review → parallel Operator + Fallback
    if swarm in ("on", "review"):
        from .swarm_runner import run_swarm_ask
        return run_swarm_ask(
            message=message,
            operator_model=model,
            fallback_model=state.get("fallback_model"),
            system_prompt=system_prompt,
            mode=swarm,
        )

    label = phase_label(swarm if swarm != "off" else "work")

    with Spinner(label):
        result = client.chat(
            message=message,
            model=model,
            system_prompt=system_prompt,
            temperature=getattr(args, "temperature", 0.7),
            max_tokens=getattr(args, "max_tokens", 4096),
            fallback_model=state.get("fallback_model") or None,
        )

    _print_response(result)
    try:
        history_record(
            kind="ask", prompt=message,
            response=result.get("response",""),
            model=result.get("model"),
            latency_ms=result.get("latency_ms") or result.get("latency"),
        )
    except Exception:
        pass
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    """Interactive multi-turn chat session. Type /exit or /quit to stop."""
    _, client = session_client()
    state = get_state()
    model = _resolve_model(state, getattr(args, "model", None))
    swarm = state.get("swarm_mode", "off")

    workspace = state.get("workspace_root")
    system_prompt = None
    if not getattr(args, "no_agents", False):
        system_prompt = read_agents_md(workspace)

    agents_hint = " [AGENTS.md loaded]" if system_prompt else ""
    print(f"ai-coder chat · model={model or 'backend default'} · swarm={swarm}{agents_hint}")
    print("Commands: /exit  /model <name>  /swarm <mode>  /status")
    print("─" * 50)

    history: list[dict] = []

    while True:
        try:
            user_input = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nSession beendet.")
            break

        if not user_input:
            continue

        # Slash-commands in session
        if user_input.startswith("/"):
            parts = user_input.split(None, 1)
            cmd = parts[0].lower()
            val = parts[1] if len(parts) > 1 else None
            if cmd in ("/exit", "/quit", "/q"):
                print("Session beendet.")
                break
            elif cmd == "/model" and val:
                model = val
                set_model(val)
                print(f"model → {val}")
            elif cmd == "/swarm" and val:
                try:
                    set_swarm(val)
                    swarm = val
                    print(f"swarm → {val}")
                except ValueError as e:
                    print(f"Fehler: {e}")
            elif cmd == "/status":
                print(f"model={model or 'backend default'}  swarm={swarm}  turns={len(history)}")
            elif cmd == "/clear":
                history.clear()
                print("History gecleart.")
            else:
                print(f"Unbekannter Command: {cmd}")
            continue

        # Build context-aware message: include last N turns
        context = ""
        if history:
            recent = history[-4:]  # last 4 turns max
            context_parts = []
            for turn in recent:
                context_parts.append(f"User: {turn['user']}")
                context_parts.append(f"Assistant: {turn['assistant']}")
            context = "\n".join(context_parts) + "\n\n"
            message = context + f"User: {user_input}"
        else:
            message = user_input

        label = phase_label(swarm if swarm != "off" else "work")
        fallback = state.get("fallback_model") or None
        with Spinner(label):
            try:
                result = client.chat(
                    message=message,
                    model=model,
                    system_prompt=system_prompt,
                    temperature=0.7,
                    max_tokens=4096,
                    fallback_model=fallback,
                )
            except (ClientError, RuntimeError) as e:
                print(f"\nFehler: {e}", file=sys.stderr)
                continue

        resp = result.get("response", "")
        model_used = result.get("model", model or "?")
        latency = result.get("latency_ms")

        print(f"\n{resp}\n")
        meta = f"[{model_used}"
        if latency:
            meta += f" · {latency}ms"
        if result.get("fallback_used"):
            meta += " · FALLBACK"
        meta += "]"
        print(meta)
        print()

        history.append({"user": user_input, "assistant": resp})
        try:
            history_record(
                kind="chat", prompt=user_input,
                response=resp, model=model_used, latency_ms=latency,
            )
        except Exception:
            pass

    return 0


# ── Task ─────────────────────────────────────────────────────────────────────

def cmd_task(args: argparse.Namespace) -> int:
    """File-aware coding task: Datei lesen → LLM → Diff → optional apply."""
    from .task import run_task
    task = " ".join(args.task) if args.task else ""
    if not task:
        print("Fehler: Kein Task angegeben.", file=sys.stderr)
        return 1
    rc = run_task(
        task=task,
        file_paths=args.files or [],
        model=args.model,
        apply=args.apply,
        dry_run=args.dry_run,
        no_agents=args.no_agents,
        temperature=args.temperature,
    )
    return rc

# ── Parser ───────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aicoder",
        description="ai-coder — terminalbasierter Coding-Agent für AILinux / TriForce",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""        Beispiele:
          aicoder login --base-url http://127.0.0.1:9000
          aicoder model anthropic/claude-sonnet-4
          aicoder fallback gemini/gemini-2.0-flash
          aicoder swarm auto
          aicoder status
          aicoder ask "Was macht diese Funktion?"
          aicoder task "Füge Docstrings hinzu" -f datei.py --dry-run
          aicoder review -f datei.py
          aicoder models --filter groq
          aicoder mcp-list
          aicoder hist
        """),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # auth
    p = sub.add_parser("login", help="Login → /v1/auth/login")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--email")
    p.add_argument("--password")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("logout", help="Lokale Session löschen")
    p.set_defaults(func=cmd_logout)

    p = sub.add_parser("whoami", help="Token prüfen → /v1/auth/verify")
    p.set_defaults(func=cmd_whoami)

    p = sub.add_parser("handshake", help="Client-Handshake abfragen")
    p.set_defaults(func=cmd_handshake)

    p = sub.add_parser("tools", help="Erlaubte Tools aus Handshake anzeigen")
    p.set_defaults(func=cmd_tools)

    p = sub.add_parser("profile", help="Lokale Sessiondaten (masked) anzeigen")
    p.set_defaults(func=cmd_profile)

    # workspace
    p = sub.add_parser("workspace", help="Lokalen Workspace/Repo analysieren")
    p.add_argument("path", nargs="?")
    p.set_defaults(func=cmd_workspace)

    # mcp
    p = sub.add_parser("mcp", help="MCP-Tool-Call → /v1/mcp")
    p.add_argument("tool")
    p.add_argument("arg", nargs="*")
    p.add_argument("--mode", default=None, help="Spinner-Modus (work/swarm/hive)")
    p.set_defaults(func=cmd_mcp)

    # session state
    p = sub.add_parser("model", help="Aktives Coding-Modell anzeigen oder setzen")
    p.add_argument("value", nargs="?")
    p.set_defaults(func=cmd_model)

    p = sub.add_parser("fallback", help="Fallback-Modell anzeigen oder setzen")
    p.add_argument("value", nargs="?")
    p.set_defaults(func=cmd_fallback)

    p = sub.add_parser("swarm", help=f"Swarm-Modus anzeigen oder setzen ({', '.join(sorted(SWARM_MODES))})")
    p.add_argument("value", nargs="?")
    p.set_defaults(func=cmd_swarm)

    p = sub.add_parser("status", help="Aktiven Status ausgeben (model, fallback, swarm, workspace, docs)")
    p.set_defaults(func=cmd_status)

    # ask / chat / task
    p = sub.add_parser("ask", help="Single-shot Prompt ans LLM senden")
    p.add_argument("prompt", nargs="*", help="Prompt-Text (oder stdin wenn leer)")
    p.add_argument("--model", default=None)
    p.add_argument("--no-agents", dest="no_agents", action="store_true")
    p.add_argument("--temperature", type=float, default=0.7)
    p.add_argument("--max-tokens", dest="max_tokens", type=int, default=4096)
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("chat", help="Interaktive Multi-Turn-Chat-Session")
    p.add_argument("--model", default=None)
    p.add_argument("--no-agents", dest="no_agents", action="store_true")
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("task", help="File-aware Coding-Task: Datei → LLM → Diff → apply")
    p.add_argument("task", nargs="*", help="Task-Beschreibung")
    p.add_argument("-f", "--file", dest="files", action="append", metavar="FILE")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--model", default=None)
    p.add_argument("--no-agents", dest="no_agents", action="store_true")
    p.add_argument("--temperature", type=float, default=0.3)
    p.set_defaults(func=cmd_task)

    p = sub.add_parser("review", help="Strukturiertes Code-Review einer Datei")
    p.add_argument("-f", "--file", dest="files", action="append", metavar="FILE")
    p.add_argument("--model", default=None)
    p.add_argument("--no-agents", dest="no_agents", action="store_true")
    p.set_defaults(func=cmd_review)

    # models / mcp-list
    p = sub.add_parser("models", help="Verfügbare Modelle vom Backend auflisten")
    p.add_argument("--filter", default=None, help="Filter by substring")
    p.add_argument("--json", dest="json_out", action="store_true")
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("mcp-list", help="Alle MCP-Tools tabellarisch anzeigen")
    p.set_defaults(func=cmd_mcp_list)

    # history
    p = sub.add_parser("hist", help="Call-History anzeigen")
    p.add_argument("-n", type=int, default=10)
    p.add_argument("--clear", action="store_true")
    p.set_defaults(func=cmd_hist)

    # debug/demo
    p = sub.add_parser("status-demo", help="Nur Statusphasen lokal testen")
    p.add_argument("--mode", default="swarm")
    p.add_argument("--seconds", type=float, default=2.0)
    p.set_defaults(func=cmd_status_demo)

    return parser


def cmd_models(args: argparse.Namespace) -> int:
    """Liste verfügbare Modelle vom Backend."""
    session, client = session_client()
    with Spinner("working..."):
        data = client._request("GET", "/v1/client/models", require_auth=True, _label="models")
    models = data.get("models", [])
    tier = data.get("tier", "?")
    count = data.get("model_count", len(models))

    if getattr(args, "filter", None):
        f = args.filter.lower()
        models = [m for m in models if f in m.lower()]

    if getattr(args, "json_out", False):
        print_json({"tier": tier, "count": len(models), "models": models})
        return 0

    print(f"tier={tier}  models={count}  showing={len(models)}")
    print("─" * 50)
    for m in models:
        print(f"  {m}")
    return 0


def cmd_mcp_list(_: argparse.Namespace) -> int:
    """Tabular list of all allowed MCP tools."""
    _, client = session_client()
    payload = {"jsonrpc": "2.0", "method": "tools/list", "params": {}, "id": 1}
    with Spinner("working..."):
        data = client._request("POST", "/v1/mcp", payload, require_auth=True, _label="tools/list")
    tools = data.get("result", {}).get("tools", [])
    print(f"{'Name':<35} {'Description'}")
    print("─" * 80)
    for t in tools:
        name = t.get("name", "")
        desc = (t.get("description", "") or "")[:60]
        print(f"  {name:<33} {desc}")
    print(f"─" * 80)
    print(f"  {len(tools)} tools")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    """Code review: Datei analysieren → strukturiertes Review."""
    from .task import run_task
    files = args.files or []
    if not files:
        print("Fehler: Mindestens eine Datei mit -f angeben.", file=sys.stderr)
        return 1
    review_prompt = (
        "Perform a structured code review. Cover: "
        "1) Bugs or logic errors "
        "2) Security issues "
        "3) Performance problems "
        "4) Code quality / readability "
        "5) Top 3 concrete improvement suggestions. "
        "Be direct and specific. No padding."
    )
    return run_task(
        task=review_prompt,
        file_paths=files,
        model=args.model,
        apply=False,
        dry_run=False,
        no_agents=args.no_agents,
        temperature=0.3,
    )


def cmd_hist(args: argparse.Namespace) -> int:
    """Show call history."""
    if getattr(args, "clear", False):
        clear_history()
        print("History gelöscht.")
        return 0
    n = getattr(args, "n", 10)
    entries = get_history(n)
    if not entries:
        print("Keine History vorhanden.")
        return 0
    for e in entries:
        ts = e.get("ts","")[:16].replace("T"," ")
        kind = e.get("kind","?")
        model = e.get("model","?")
        lat = e.get("latency_ms","?")
        prompt = e.get("prompt","")[:80].replace("\n"," ")
        files = e.get("files",[])
        fstr = f" [{', '.join(files[:2])}]" if files else ""
        print(f"  {ts}  {kind:<6} {model:<40} {lat}ms")
        print(f"    └ {prompt}{fstr}")
    return 0


    p = sub.add_parser("models", help="Verfügbare Modelle vom Backend auflisten")
    p.add_argument("--filter", default=None, help="Filter by substring")
    p.add_argument("--json", dest="json_out", action="store_true")
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("mcp-list", help="Alle MCP-Tools tabellarisch anzeigen")
    p.set_defaults(func=cmd_mcp_list)

    p = sub.add_parser("review", help="Strukturiertes Code-Review einer Datei")
    p.add_argument("-f", "--file", dest="files", action="append", metavar="FILE")
    p.add_argument("--model", default=None)
    p.add_argument("--no-agents", dest="no_agents", action="store_true")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("hist", help="Call-History anzeigen")
    p.add_argument("-n", type=int, default=10, help="Anzahl Einträge")
    p.add_argument("--clear", action="store_true", help="History löschen")
    p.set_defaults(func=cmd_hist)

    # debug/demo
    p = sub.add_parser("status-demo", help="Nur Statusphasen lokal testen")
    p.add_argument("--mode", default="swarm")
    p.add_argument("--seconds", type=float, default=2.0)
    p.set_defaults(func=cmd_status_demo)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args) or 0)
    except (ClientError, RuntimeError) as e:
        print(f"Fehler: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Abgebrochen.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
