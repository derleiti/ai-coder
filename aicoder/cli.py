from __future__ import annotations
import argparse, json, os, sys, textwrap, time
from getpass import getpass
from pathlib import Path
from typing import Any, Dict
from .client import ClientError, TriForceClient, model_identifier
from .config import DEFAULT_BASE_URL, Session, delete_session, load_session, save_session
from .docs_context import context_summary, read_agents_md
from .history import record as history_record, get_history, clear_history
from .session_state import (
    RUNTIME_MODES, DEFAULT_RUNTIME_MODE, SWARM_MODES, TOOL_MODES, get_state,
    set_fallback, set_model, set_runtime_mode, set_swarm, set_tool_mode, set_workspace,
)
from .status import Spinner, phase_label
from .workspace import activate_workspace, active_workspace, workspace_snapshot
from .tool_policy import (
    filter_tool_catalog,
    require_allowed_tool,
)


def parse_kv_pairs(pairs: list[str]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for item in pairs:
        if "=" not in item:
            raise ClientError(f"Invalid argument '{item}'. Expected key=value")
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
    password = getpass("Passwort: ")  # kein --password Flag (Security: Shell-History)
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
    print("Session deleted.")
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
    from .executor import AGENT_TOOLS
    _, client = session_client()
    data = client.handshake()
    tools = data.get("tools") or []
    allowed = data.get("allowed_tools") or []

    # Admin/enterprise handshakes commonly grant the wildcard instead of
    # embedding the full tool catalog. Resolve it through MCP tools/list so
    # this command reports the same effective tools as the GUI/agent.
    if not tools and "*" in allowed:
        payload = {"jsonrpc": "2.0", "method": "tools/list", "params": {}, "id": 1}
        listed = client._request(
            "POST", "/v1/mcp", payload, require_auth=True, _label="tools/list"
        )
        tools = listed.get("result", {}).get("tools", [])
    elif not tools:
        tools = allowed

    if tools and isinstance(tools[0], dict):
        tools = filter_tool_catalog(tools, AGENT_TOOLS)
    else:
        tools = [name for name in tools if name in AGENT_TOOLS and require_allowed_tool(name, AGENT_TOOLS)[0]]

    print(f"{len(tools)} tools allowed")
    for tool in tools:
        if isinstance(tool, dict):
            print(tool.get("name", ""))
        else:
            print(tool)
    return 0


def cmd_profile(_: argparse.Namespace) -> int:
    session = load_session()
    print_json(session.masked())
    return 0


def cmd_workspace(args: argparse.Namespace) -> int:
    root = activate_workspace(args.path)
    set_workspace(str(root))
    snap = workspace_snapshot(str(root))
    print_json(snap)
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from .agent import _cli_approval
    from .executor import AGENT_TOOLS, load_tools, run_tool

    _, client = session_client()
    arguments = parse_kv_pairs(args.arg or [])
    allowed, reason = require_allowed_tool(args.tool, AGENT_TOOLS)
    if not allowed:
        print(f"Error: {reason}", file=sys.stderr)
        return 2
    # Populate backend mutation annotations before routing through the same
    # approval/audit path as GUI and agent calls.
    load_tools(client)
    state = get_state()
    # Show active context before running
    swarm = state.get('swarm_mode', 'off')
    _print_header(state)
    label = phase_label(args.mode or swarm)
    with Spinner(label):
        output, is_error = run_tool(
            client, args.tool, arguments,
            approval_fn=_cli_approval,
            model="user/direct-mcp",
            allowed_tools=set(AGENT_TOOLS),
        )
    print(output)
    return 1 if is_error else 0


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
        effective = get_state().get("fallback_model") or ""
        if effective:
            print(f"fallback → {effective}")
        else:
            print("fallback → disabled (same as operator)")
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
            print(f"Error: {e}", file=sys.stderr)
            return 1
    else:
        state = get_state()
        print(f"swarm = {state.get('swarm_mode', 'off')}")
    return 0


def cmd_tool_mode(args: argparse.Namespace) -> int:
    value = getattr(args, "value", None)
    if value:
        try:
            set_tool_mode(value)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print(f"tool-mode → {value}")
    else:
        print(f"tool-mode = {get_state().get('tool_mode', 'on_demand')}")
    return 0

def cmd_runtime(args: argparse.Namespace) -> int:
    value = getattr(args, "value", None)
    if value:
        try:
            set_runtime_mode(value)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print(f"runtime → {value}")
    else:
        print(f"runtime = {get_state().get('runtime_mode', DEFAULT_RUNTIME_MODE)}")
    return 0


def cmd_skills(args: argparse.Namespace) -> int:
    from .skills import discover_skills, read_skill

    state = get_state()
    workspace = str(active_workspace(state.get("workspace_root")))
    name = getattr(args, "name", None)
    if name:
        text, is_error = read_skill(workspace, name)
        stream = sys.stderr if is_error else sys.stdout
        print(text, file=stream)
        return 1 if is_error else 0
    skills = discover_skills(workspace)
    if not skills:
        print("no skills discovered")
        return 0
    for skill in skills:
        print(f"{skill.name:<24} {skill.scope:<18} {skill.description}")
    return 0



def cmd_guidelines(args: argparse.Namespace) -> int:
    from .guidelines import load_guidelines

    state = get_state()
    workspace = str(active_workspace(state.get("workspace_root")))
    rows = load_guidelines(workspace)
    if not rows:
        print("no guidelines discovered")
        return 0
    for scope, text in rows:
        print(f"## {scope}\n{text}\n")
    return 0


def cmd_commands(args: argparse.Namespace) -> int:
    from .commands import discover_commands, read_command

    state = get_state()
    workspace = str(active_workspace(state.get("workspace_root")))
    name = getattr(args, "name", None)
    if name:
        text, is_error = read_command(workspace, name)
        print(text, file=sys.stderr if is_error else sys.stdout)
        return 1 if is_error else 0
    commands = discover_commands(workspace)
    if not commands:
        print("no commands discovered")
        return 0
    for command in commands:
        print(f"{command.name:<24} {command.scope:<18} {command.description}")
    return 0


def cmd_command(args: argparse.Namespace) -> int:
    from .agent import run_agent
    from .commands import expand_command

    state = get_state()
    workspace = str(active_workspace(state.get("workspace_root")))
    text, is_error = expand_command(
        workspace,
        str(getattr(args, "name", "") or ""),
        " ".join(getattr(args, "arguments", []) or []),
    )
    if is_error:
        print(text, file=sys.stderr)
        return 1
    return run_agent(
        initial_prompt=text,
        model=getattr(args, "model", None) or state.get("selected_model"),
        fallback_model=state.get("fallback_model"),
        verbose=getattr(args, "verbose", False),
        runtime_mode="native-light",
        json_output=bool(getattr(args, "json_out", False)),
        json_events=bool(getattr(args, "json_events", False)),
    )

def cmd_plan(args: argparse.Namespace) -> int:
    from .agent_plan import PlanStore, format_plan

    state = get_state()
    workspace = str(active_workspace(state.get("workspace_root")))
    store = PlanStore()
    if getattr(args, "clear", False):
        cleared = store.clear_current(workspace)
        print("current plan cleared" if cleared else "no current plan")
        return 0
    if getattr(args, "list", False):
        plans = store.list(workspace, limit=getattr(args, "limit", 10))
        if not plans:
            print("no plans")
            return 0
        for plan in plans:
            print(f"{plan.id}  {plan.status:<9}  iter={plan.iteration:<3}  {plan.task[:80]}")
        return 0
    plan_id = getattr(args, "id", None)
    plan = store.load(workspace, plan_id) if plan_id else store.load_current(workspace)
    if plan is None:
        print("no current plan")
        return 1
    print(format_plan(plan))
    try:
        from .agent_journal import ContinuationJournalStore
        journal = ContinuationJournalStore(store.root.parent / "journals").load(workspace, plan.id)
    except (OSError, ValueError):
        journal = None
    if journal is not None:
        print(f"journal=present  messages={len(journal.messages)}  tool_batches={len(journal.tool_batches)}")
    else:
        print("journal=none")
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    state = get_state()
    ctx = context_summary(str(active_workspace(state.get("workspace_root"))))

    model = state.get("selected_model") or "(not set)"
    fallback = state.get("fallback_model") or "(not set)"
    swarm = state.get("swarm_mode", "off")
    workspace = str(active_workspace(state.get("workspace_root")))

    print("── ai-coder status ──────────────────────────────")
    print(f"  model    : {model}")
    print(f"  fallback : {fallback}")
    print(f"  swarm    : {swarm}")
    print(f"  runtime  : {state.get('runtime_mode', DEFAULT_RUNTIME_MODE)}")
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
    session = load_session()
    _timeout = getattr(args, "timeout", 90)
    client = TriForceClient(session.base_url, token=session.token, timeout=_timeout)
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
    workspace = str(active_workspace(state.get("workspace_root")))
    system_prompt = None
    if not getattr(args, "no_agents", False):
        system_prompt = read_agents_md(workspace)

    _print_header(state, model)

    # Swarm V2: on|review → parallel; auto → Heuristik
    _effective_swarm = swarm
    if swarm == "auto":
        from .swarm_runner import should_auto_swarm
        if should_auto_swarm(message):
            _effective_swarm = "on"
            print("swarm: auto-triggered (complex prompt)", file=sys.stderr)

    if _effective_swarm in ("on", "review"):
        from .swarm_runner import run_swarm_ask
        return run_swarm_ask(
            message=message,
            operator_model=model,
            fallback_model=state.get("fallback_model"),
            system_prompt=system_prompt,
            mode=_effective_swarm,
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
    session = load_session()
    client = TriForceClient(session.base_url, token=session.token, timeout=120)
    state = get_state()
    model = _resolve_model(state, getattr(args, "model", None))
    swarm = state.get("swarm_mode", "off")

    workspace = str(active_workspace(state.get("workspace_root")))
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
            print("\nSession ended.")
            break

        if not user_input:
            continue

        # Slash-commands in session
        if user_input.startswith("/"):
            parts = user_input.split(None, 1)
            cmd = parts[0].lower()
            val = parts[1] if len(parts) > 1 else None
            if cmd in ("/exit", "/quit", "/q"):
                print("Session ended.")
                break
            elif cmd == "/model" and val:
                model = val
                set_model(val)
                state = get_state()
                print(f"model → {val}")
            elif cmd == "/swarm" and val:
                try:
                    set_swarm(val)
                    swarm = val
                    print(f"swarm → {val}")
                except ValueError as e:
                    print(f"Error: {e}")
            elif cmd == "/status":
                print(f"model={model or 'backend default'}  swarm={swarm}  turns={len(history)}")
            elif cmd == "/fallback" and val:
                set_fallback(val)
                effective = get_state().get("fallback_model") or ""
                state["fallback_model"] = effective
                if effective:
                    print(f"fallback → {effective}")
                else:
                    print("fallback → disabled (same as operator)")
            elif cmd == "/help":
                print("  /model <n>  /fallback <n>  /swarm <mode>  /status  /clear  /exit")
            elif cmd == "/clear":
                history.clear()
                print("History cleared.")
            else:
                print(f"Unknown command: {cmd}")
            continue

        # Build proper messages array for multi-turn context
        # Limit: keep last 6 turns but cap each response to 2000 chars
        # to avoid context window explosion on long sessions
        chat_messages = []
        if history:
            for turn in history[-6:]:
                chat_messages.append({"role": "user", "content": turn["user"][:2000]})
                resp_trimmed = turn["assistant"]
                if len(resp_trimmed) > 2000:
                    resp_trimmed = resp_trimmed[:1900] + "\n[...truncated for context]"
                chat_messages.append({"role": "assistant", "content": resp_trimmed})
        chat_messages.append({"role": "user", "content": user_input})

        # Auto-swarm heuristik
        _cs = swarm
        if swarm == "auto":
            from .swarm_runner import should_auto_swarm
            if should_auto_swarm(user_input):
                _cs = "on"
        label = phase_label(_cs if _cs != "off" else "work")
        fallback = state.get("fallback_model") or None
        with Spinner(label):
            try:
                result = client.chat(
                    messages=chat_messages,
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

        if _cs in {"on", "review"}:
            from .swarm_runner import run_swarm_review
            run_swarm_review(
                original_task=user_input,
                operator_response=resp,
                operator_model=model_used,
                fallback_model=fallback,
                system_prompt=system_prompt,
                client=client,
            )

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
    """File-aware coding task: read file → LLM → diff → optional apply."""
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


def cmd_init(args: argparse.Namespace) -> int:
    """Initialize workspace: create AGENTS.md, set workspace_root."""
    import subprocess
    from pathlib import Path as _P
    target = _P(getattr(args, "path", None) or os.getcwd()).resolve()
    target.mkdir(parents=True, exist_ok=True)
    set_workspace(str(target))
    print(f"workspace -> {target}")
    if not (target / ".git").exists() and not getattr(args, "no_git", False):
        subprocess.run(["git", "init", str(target)], capture_output=True)
        print("git init OK")
    agents_path = target / "AGENTS.md"
    if agents_path.exists() and not getattr(args, "force", False):
        print("AGENTS.md already exists -- skip (--force to overwrite)")
    else:
        proj_name = target.name
        lines_t = [
            "# AGENTS.md -- " + proj_name, "",
            "Operational instructions for ai-coder.", "",
            "## Rules", "",
            "1. Root cause before fix.",
            "2. Small robust changes.",
            "3. Read-first.",
            "4. State uncertainty.", "",
            "## Stack", "", "- TODO: Add technologies", "",
            "## Conventions", "", "- TODO: Add code style", "",
        ]
        agents_path.write_text("\n".join(lines_t), encoding="utf-8")
        print(f"AGENTS.md OK ({agents_path})")
    gi = target / ".gitignore"
    if not gi.exists():
        gi_lines = ["__pycache__/", "*.pyc", ".venv/", ".env", "*.egg-info/", ""]
        gi.write_text("\n".join(gi_lines), encoding="utf-8")
        print(".gitignore OK")
    print("\nDone. Next: aicoder status")
    return 0


def cmd_broadcast(args: argparse.Namespace) -> int:
    """Swarm broadcast: send question to all backend models via swarm_broadcast MCP."""
    _, client = session_client()
    question = " ".join(args.question) if args.question else ""
    if not question:
        print("Error: provide a question.", file=sys.stderr)
        return 1
    providers = getattr(args, "providers", None) or None
    skip = getattr(args, "skip", None) or None
    top_n = getattr(args, "top_n", 5)
    max_tokens = getattr(args, "max_tokens", 200)
    params: dict = {"question": question, "max_tokens": max_tokens, "top_n": top_n}
    if providers:
        params["only_providers"] = [p.strip() for p in providers.split(",")]
    if skip:
        params["skip_providers"] = [p.strip() for p in skip.split(",")]
    print(f"Broadcasting (top_n={top_n}, providers={params.get('only_providers','all')})...", file=sys.stderr)
    with Spinner("swarming..."):
        try:
            raw = client.mcp_call("swarm_broadcast", params, allow_internal=True)
        except ClientError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
    content = raw.get("result", {}).get("content", [{}])[0].get("text", "{}")
    try:
        data = json.loads(content)
    except Exception:
        print(content)
        return 0
    s = data.get("session", {})
    print(f"\nSwarm {s.get('id','?')} -- {s.get('responses_count',0)} responses in {s.get('elapsed_ms',0)}ms")
    print("-" * 60)
    for i, r in enumerate(data.get("top_results", []), 1):
        print(f"\n#{i} [{r.get('model_id','?')}  score={r.get('quality_score',0):.3f}  {r.get('latency_ms','?')}ms]")
        print(r.get("response", "").strip())
    try:
        best = data.get("top_results", [{}])[0].get("response", "")
        history_record(kind="ask", prompt=question, response=best,
                       model="swarm/" + s.get("id","?"), latency_ms=s.get("elapsed_ms"))
    except Exception:
        pass
    return 0


def cmd_shell(args: argparse.Namespace) -> int:
    """Retained as a compatibility stub; shell execution is out of scope."""
    print(
        "Error: remote shell/binary execution is disabled by the ai-coder coding-only policy.",
        file=sys.stderr,
    )
    return 2



def cmd_sysinfo(args: argparse.Namespace) -> int:
    """System overview: local (--local) or backend via safe_probe."""
    import shutil, subprocess as sp

    if getattr(args, "local", False):
        # Lokale System-Info via subprocess — laeuft auf DIESEM Rechner
        print(f"\033[1m\033[96mLocal system info\033[0m  \033[2m({os.uname().nodename})\033[0m")
        print("\033[2m" + "─" * 50 + "\033[0m")
        cmds = {
            "uptime":   ["uptime"],
            "ram":      ["free", "-h"],
            "disk":     ["df", "-h", "--total", "-x", "tmpfs", "-x", "devtmpfs"],
            "cpu":      ["cat", "/proc/cpuinfo"],
            "load":     ["cat", "/proc/loadavg"],
        }
        if getattr(args, "probe", None):
            p = args.probe
            if p in cmds:
                cmds = {p: cmds[p]}
            else:
                print(f"Error: unknown read-only probe: {p}", file=sys.stderr)
                return 2
        for label, cmd in cmds.items():
            if label == "cpu":
                # CPU kompakt
                try:
                    out = sp.check_output(["grep", "-m1", "model name", "/proc/cpuinfo"],
                                          text=True, timeout=3).strip().split(":")[1].strip()
                    cores = sp.check_output(["nproc"], text=True, timeout=3).strip()
                    print(f"  \033[36mcpu\033[0m       {out} ({cores} cores)")
                except Exception:
                    pass
                continue
            try:
                out = sp.check_output(cmd, text=True, timeout=5).strip()
                print(f"  \033[36m{label}\033[0m")
                for line in out.splitlines()[:15]:
                    print(f"    {line}")
            except FileNotFoundError:
                print(f"  {label}: command not found")
            except Exception as e:
                print(f"  {label}: {e}")
        return 0

    # Remote infrastructure probing is outside the coding-client scope.
    print("Error: remote system probing is disabled; use --local for local read-only stats.", file=sys.stderr)
    return 2


def cmd_service(args: argparse.Namespace) -> int:
    """Retained as a compatibility stub; service management is out of scope."""
    print("Error: service management is disabled by the ai-coder coding-only policy.", file=sys.stderr)
    return 2


def cmd_remote_node(args: argparse.Namespace) -> int:
    """Expose the active workspace to TriForce through the read-only preview node."""
    from .remote_node import run_remote_node

    state = get_state()
    workspace = str(active_workspace(state.get("workspace_root")))
    allow_writes = bool(getattr(args, "allow_writes", False))
    profile = "write-preview" if allow_writes else "read-only"
    print(f"remote-node · {profile} · workspace={workspace}", file=sys.stderr)
    if allow_writes:
        print(
            "Remote file create/exact-replace enabled; backups are mandatory. "
            "Delete, shell and blind overwrite remain blocked.",
            file=sys.stderr,
        )
    else:
        print("Ctrl+C stops the remote node. Writes and shell execution are blocked.", file=sys.stderr)
    try:
        run_remote_node(allow_writes=allow_writes)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"remote-node error: {exc}", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aicoder",
        description="ai-coder — terminal-based coding agent for AILinux / TriForce",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""        Examples:
          aicoder login --base-url http://127.0.0.1:9000
          aicoder model anthropic/claude-sonnet-4
          aicoder fallback gemini/gemini-2.0-flash
          aicoder swarm auto
          aicoder status
          aicoder ask "Was macht diese Funktion?"
          aicoder task "Add docstrings" -f datei.py --dry-run
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
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("logout", help="Delete local session")
    p.set_defaults(func=cmd_logout)

    p = sub.add_parser("whoami", help="Verify token → /v1/auth/verify")
    p.set_defaults(func=cmd_whoami)

    p = sub.add_parser("handshake", help="Query client handshake")
    p.set_defaults(func=cmd_handshake)

    p = sub.add_parser("tools", help="Show allowed tools from handshake")
    p.set_defaults(func=cmd_tools)

    p = sub.add_parser("profile", help="Show local session data (masked)")
    p.set_defaults(func=cmd_profile)

    # workspace
    p = sub.add_parser("workspace", help="Analyze local workspace/repo")
    p.add_argument("path", nargs="?")
    p.set_defaults(func=cmd_workspace)

    # mcp
    p = sub.add_parser("mcp", help="MCP-Tool-Call → /v1/mcp")
    p.add_argument("tool")
    p.add_argument("arg", nargs="*")
    p.add_argument("--mode", default=None, help="Spinner-Modus (work/swarm/hive)")
    p.set_defaults(func=cmd_mcp)

    # session state
    p = sub.add_parser("model", help="Show or set active coding model")
    p.add_argument("value", nargs="?")
    p.set_defaults(func=cmd_model)

    p = sub.add_parser("fallback", help="Show or set fallback model")
    p.add_argument("value", nargs="?")
    p.set_defaults(func=cmd_fallback)

    p = sub.add_parser("swarm", help=f"Swarm-Modus anzeigen oder setzen ({', '.join(sorted(SWARM_MODES))})")
    p.add_argument("value", nargs="?")
    p.set_defaults(func=cmd_swarm)


    p = sub.add_parser("tool-mode", help=f"Tool discovery mode ({', '.join(sorted(TOOL_MODES))})")
    p.add_argument("value", nargs="?")
    p.set_defaults(func=cmd_tool_mode)

    p = sub.add_parser("runtime", help=f"Agent runtime ({', '.join(sorted(RUNTIME_MODES))})")
    p.add_argument("value", nargs="?")
    p.set_defaults(func=cmd_runtime)

    p = sub.add_parser("skills", help="List or read native AICoder workflow skills")
    p.add_argument("name", nargs="?", help="Skill name to read")
    p.set_defaults(func=cmd_skills)


    p = sub.add_parser("guidelines", help="Show effective native AICoder guidelines")
    p.set_defaults(func=cmd_guidelines)

    p = sub.add_parser("commands", help="List or read native AICoder prompt commands")
    p.add_argument("name", nargs="?", help="Command name to read")
    p.set_defaults(func=cmd_commands)

    p = sub.add_parser("command", help="Run a native AICoder prompt command")
    p.add_argument("name", help="Command name")
    p.add_argument("arguments", nargs="*", help="Arguments passed to $ARGUMENTS/{{args}}")
    p.add_argument("--model", default=None)
    p.add_argument("--verbose", "-v", action="store_true")
    headless = p.add_mutually_exclusive_group()
    headless.add_argument("--json", dest="json_out", action="store_true", help="Headless final JSON output")
    headless.add_argument("--json-events", action="store_true", help="Headless NDJSON runtime events")
    p.set_defaults(func=cmd_command)

    p = sub.add_parser("plan", help="Show/list the persistent native-light execution plan")
    p.add_argument("id", nargs="?", help="Specific plan id (default: current)")
    p.add_argument("--list", action="store_true", help="List recent plans for the workspace")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--clear", action="store_true", help="Clear only the current-plan pointer")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("status", help="Show active status (model, fallback, swarm, workspace, docs)")
    p.set_defaults(func=cmd_status)

    # ask / chat / task
    p = sub.add_parser("ask", help="Send single-shot prompt to LLM")
    p.add_argument("prompt", nargs="*", help="Prompt text (or stdin if empty)")
    p.add_argument("--model", default=None)
    p.add_argument("--no-agents", dest="no_agents", action="store_true")
    p.add_argument("--temperature", type=float, default=0.7)
    p.add_argument("--max-tokens", dest="max_tokens", type=int, default=4096)
    p.add_argument("--timeout", type=int, default=90, help="HTTP timeout in seconds")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("chat", help="Interactive multi-turn chat session")
    p.add_argument("--model", default=None)
    p.add_argument("--no-agents", dest="no_agents", action="store_true")
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("task", help="File-aware coding task: file → LLM → diff → apply")
    p.add_argument("task", nargs="*", help="Task description")
    p.add_argument("-f", "--file", dest="files", action="append", metavar="FILE")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--model", default=None)
    p.add_argument("--no-agents", dest="no_agents", action="store_true")
    p.add_argument("--temperature", type=float, default=0.3)
    p.add_argument("--timeout", type=int, default=90, help="HTTP timeout in seconds")
    p.set_defaults(func=cmd_task)

    p = sub.add_parser("review", help="Structured code review of a file")
    p.add_argument("-f", "--file", dest="files", action="append", metavar="FILE")
    p.add_argument("--model", default=None)
    p.add_argument("--no-agents", dest="no_agents", action="store_true")
    p.set_defaults(func=cmd_review)

    # models / mcp-list
    p = sub.add_parser("models", help="List available models from backend")
    p.add_argument("--filter", default=None, help="Filter by substring")
    p.add_argument("--group", action="store_true", help="Nach Provider gruppieren")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--json", dest="json_out", action="store_true")
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("mcp-list", help="List all MCP tools in table format")
    p.set_defaults(func=cmd_mcp_list)

    # history
    p = sub.add_parser("hist", help="Show call history")
    p.add_argument("-n", type=int, default=10)
    p.add_argument("--clear", action="store_true")
    p.set_defaults(func=cmd_hist)

    p = sub.add_parser("init", help="Initialize workspace + create AGENTS.md")
    p.add_argument("path", nargs="?")
    p.add_argument("--force", action="store_true")
    p.add_argument("--no-git", dest="no_git", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("broadcast", help="Swarm broadcast to all backend models")
    p.add_argument("question", nargs="*")
    p.add_argument("--providers", default=None, help="Comma-separated: groq,mistral")
    p.add_argument("--skip", default=None)
    p.add_argument("--top-n", dest="top_n", type=int, default=5)
    p.add_argument("--max-tokens", dest="max_tokens", type=int, default=200)
    p.set_defaults(func=cmd_broadcast)

    p = sub.add_parser("shell", help="Disabled compatibility stub (coding-only policy)")
    p.add_argument("cmd", nargs="*")
    p.add_argument("--raw", "-r", action="store_true", help="Shell tool instead of binary_exec (pipes etc.)")
    p.add_argument("--elevated", "-e", action="store_true")
    p.add_argument("--cwd", default=None)
    p.add_argument("--timeout", type=int, default=30)
    p.set_defaults(func=cmd_shell)


    p = sub.add_parser("sysinfo", help="Read-only local system overview (--local required)")
    p.add_argument("action", nargs="?", default="overview",
                   choices=["overview","run","service_status","journal","list"])
    p.add_argument("--probe", default=None)
    p.add_argument("--service", default=None)
    p.add_argument("--local", "-l", action="store_true", help="Local stats (this machine, no MCP)")
    p.set_defaults(func=cmd_sysinfo)

    p = sub.add_parser("service", help="Disabled compatibility stub (coding-only policy)")
    p.add_argument("action", choices=["status","start","stop","restart","logs","list"])
    p.add_argument("service", nargs="?", default=None)
    p.add_argument("--lines", type=int, default=50)
    p.set_defaults(func=cmd_service)

    p = sub.add_parser("remote-node", help="Expose active workspace to TriForce (safe remote preview)")
    p.add_argument(
        "--allow-writes", action="store_true",
        help="Opt in to remote create/exact-replace with mandatory backups",
    )
    p.set_defaults(func=cmd_remote_node)

    p = sub.add_parser("agent", help="Agent REPL / autonomous terminal agent")
    p.add_argument("prompt", nargs="*", help="Direct prompt (no REPL)")
    p.add_argument("--model", default=None)
    p.add_argument(
        "--resume", action="store_true",
        help="Resume the current persistent native-light plan (implies native-light)",
    )
    p.add_argument(
        "--plan-id", default=None,
        help="Resume a specific persistent plan id (requires --resume)",
    )
    p.add_argument("--setup", action="store_true")
    p.add_argument("--verbose", "-v", action="store_true")
    headless = p.add_mutually_exclusive_group()
    headless.add_argument("--json", dest="json_out", action="store_true", help="Headless final JSON output")
    headless.add_argument("--json-events", action="store_true", help="Headless NDJSON runtime events")
    p.set_defaults(func=cmd_agent)


    # GUI
    p = sub.add_parser("gui", help="Start GUI window (PyQt6)")
    p.set_defaults(func=lambda _: _run_gui())
    return parser


def cmd_agent(args: argparse.Namespace) -> int:
    """Start agent REPL (optional: direct prompt as argument)."""
    from .setup import run_repl, run_setup
    from .agent import run_agent

    # --setup Flag: nur Wizard, dann REPL
    if getattr(args, "setup", False):
        run_setup(force=True)

    prompt_parts = getattr(args, "prompt", []) or []
    resume_requested = bool(getattr(args, "resume", False))
    plan_id = getattr(args, "plan_id", None)
    if plan_id and not resume_requested:
        print("Error: --plan-id requires --resume", file=sys.stderr)
        return 2
    headless_requested = bool(getattr(args, "json_out", False) or getattr(args, "json_events", False))
    if headless_requested and not (prompt_parts or resume_requested):
        print("Error: headless agent mode requires a prompt or --resume", file=sys.stderr)
        return 2
    if prompt_parts or resume_requested:
        # Direct prompt or explicit process-restart resume: no REPL.
        from .session_state import get_state
        state = get_state()
        initial_prompt = " ".join(prompt_parts) if prompt_parts else "continue"
        return run_agent(
            initial_prompt=initial_prompt,
            model=getattr(args, "model", None) or state.get("selected_model"),
            fallback_model=state.get("fallback_model"),
            verbose=getattr(args, "verbose", False),
            runtime_mode="native-light" if resume_requested else None,
            resume_plan_id=(plan_id or "current") if resume_requested else None,
            json_output=bool(getattr(args, "json_out", False)),
            json_events=bool(getattr(args, "json_events", False)),
        )
    return run_repl(skip_setup=getattr(args, "setup", False))

def cmd_models(args: argparse.Namespace) -> int:
    """List available models from backend."""
    session, client = session_client()
    with Spinner("working..."):
        data = client._request("GET", "/v1/client/models", require_auth=True, _label="models")
    models = [
        model_id for item in data.get("models", [])
        if (model_id := model_identifier(item))
    ]
    tier = data.get("tier", "?")
    count = data.get("model_count", len(models))

    if getattr(args, "filter", None):
        f = args.filter.lower()
        models = [m for m in models if f in m.lower()]

    if getattr(args, "json_out", False):
        print_json({"tier": tier, "count": len(models), "models": models})
        return 0

    if getattr(args, "group", False):
        groups: dict = {}
        for m in models:
            prefix = m.split("/")[0] if "/" in m else "other"
            groups.setdefault(prefix, []).append(m)
        print(f"tier={tier}  total={count}  providers={len(groups)}")
        print("-" * 50)
        for provider, mlist in sorted(groups.items()):
            print(f"  [{provider}]  {len(mlist)} models")
            if getattr(args, "verbose", False):
                for mm in mlist:
                    print(f"    {mm}")
        return 0

    print(f"tier={tier}  models={count}  showing={len(models)}")
    print("-" * 50)
    for m in models:
        print(f"  {m}")
    return 0


def cmd_mcp_list(_: argparse.Namespace) -> int:
    """Tabular list of all allowed MCP tools."""
    from .executor import AGENT_TOOLS
    _, client = session_client()
    payload = {"jsonrpc": "2.0", "method": "tools/list", "params": {}, "id": 1}
    with Spinner("working..."):
        data = client._request("POST", "/v1/mcp", payload, require_auth=True, _label="tools/list")
    tools = filter_tool_catalog(data.get("result", {}).get("tools", []), AGENT_TOOLS)
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
    """Code review: analyze file → structured review."""
    from .task import run_task
    files = args.files or []
    if not files:
        print("Error: specify at least one file with -f.", file=sys.stderr)
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
        print("History cleared.")
        return 0
    n = getattr(args, "n", 10)
    entries = get_history(n)
    if not entries:
        print("No history found.")
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



def _run_gui() -> int:
    """Start the PyQt6 GUI."""
    try:
        from .gui.app import run_gui
        return run_gui()
    except ImportError as e:
        print(f"PyQt6 not installed: {e}", file=sys.stderr)
        print("Install with: pip install PyQt6", file=sys.stderr)
        return 1


def main() -> int:
    # The directory AICoder is launched from is the active process workspace.
    # Persisted workspace state must never silently override an explicit shell cwd.
    activate_workspace()
    # Kein Argument → Setup-Wizard + Agent-REPL starten
    if len(sys.argv) == 1:
        from .setup import run_repl
        return run_repl()

    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args) or 0)
    except (ClientError, RuntimeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Aborted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
