from __future__ import annotations
import argparse, json, os, sys, textwrap, time
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
    workspace = state.get("workspace_root")
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
            elif cmd == "/fallback" and val:
                state["fallback_model"] = val
                set_fallback(val)
                print(f"fallback → {val}")
            elif cmd == "/help":
                print("  /model <n>  /fallback <n>  /swarm <mode>  /status  /clear  /exit")
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


def cmd_init(args: argparse.Namespace) -> int:
    """Workspace initialisieren: AGENTS.md anlegen, workspace_root setzen."""
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
        print("AGENTS.md bereits vorhanden -- skip (--force zum Ueberschreiben)")
    else:
        proj_name = target.name
        lines_t = [
            "# AGENTS.md -- " + proj_name, "",
            "Operative Anweisungen fuer ai-coder.", "",
            "## Regeln", "",
            "1. Ursache vor Fix.",
            "2. Kleine robuste Aenderungen.",
            "3. Read-first.",
            "4. Unsicherheit benennen.", "",
            "## Stack", "", "- TODO: Technologien eintragen", "",
            "## Konventionen", "", "- TODO: Code-Style eintragen", "",
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
    """Swarm-Broadcast: Frage an alle Backend-Modelle via swarm_broadcast MCP."""
    _, client = session_client()
    question = " ".join(args.question) if args.question else ""
    if not question:
        print("Fehler: Frage angeben.", file=sys.stderr)
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
            raw = client.mcp_call("swarm_broadcast", params)
        except ClientError as e:
            print(f"Fehler: {e}", file=sys.stderr)
            return 1
    content = raw.get("result", {}).get("content", [{}])[0].get("text", "{}")
    try:
        data = json.loads(content)
    except Exception:
        print(content)
        return 0
    s = data.get("session", {})
    print(f"\nSwarm {s.get('id','?')} -- {s.get('responses_count',0)} Antworten in {s.get('elapsed_ms',0)}ms")
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
    """Shell-Befehl ueber MCP binary_exec/shell ausfuehren.

    Kurzbefehle: aicoder shell uptime
                 aicoder shell df -h
                 aicoder shell systemctl status triforce
                 aicoder shell --raw "ps aux | grep python"  (via shell tool)
    """
    _, client = session_client()
    cmd_parts = list(args.cmd) if args.cmd else []
    if not cmd_parts:
        # Ohne Argumente: liste verfuegbare Programme
        with Spinner("working..."):
            raw = client.mcp_call("binary_exec", {"action": "list"})
        content = raw.get("result", {}).get("content", [{}])[0].get("text", "")
        try:
            data = json.loads(content)
            bins = sorted(data.get("binaries", {}).keys())
            print(f"{len(bins)} verfuegbare Programme:")
            print("  ".join(bins))
        except Exception:
            print(content)
        return 0

    use_raw = getattr(args, "raw", False)

    if use_raw:
        # Rohe Shell-Ausführung via shell-Tool
        cmd_str = " ".join(cmd_parts)
        print(f"$ {cmd_str}", file=sys.stderr)
        with Spinner("working..."):
            try:
                raw = client.mcp_call("shell", {"command": cmd_str})
            except ClientError as e:
                print(f"Fehler: {e}", file=sys.stderr)
                return 1
    else:
        # binary_exec: erstes Token = Programm, Rest = Argumente
        program = cmd_parts[0]
        arguments = cmd_parts[1:]
        params: dict = {
            "action": "run",
            "program": program,
            "arguments": arguments,
            "elevated": getattr(args, "elevated", False),
            "timeout": getattr(args, "timeout", 30),
        }
        wd = getattr(args, "cwd", None)
        if wd:
            params["work_dir"] = wd
        print(f"$ {program} {' '.join(arguments)}", file=sys.stderr)
        with Spinner("working..."):
            try:
                raw = client.mcp_call("binary_exec", params)
            except ClientError as e:
                print(f"Fehler: {e}", file=sys.stderr)
                return 1

    content = raw.get("result", {}).get("content", [{}])[0].get("text", "")
    try:
        data = json.loads(content)
        out = data.get("stdout", "") or data.get("output", "") or data.get("result", "") or content
        err = data.get("stderr", "")
        rc = int(data.get("returncode", data.get("exit_code", data.get("rc", 0))) or 0)
    except Exception:
        out, err, rc = content, "", 0
    if out:
        print(out)
    if err:
        print(err, file=sys.stderr)
    return rc


def cmd_sudo(args: argparse.Namespace) -> int:
    """Sudo-Befehl: fragt Passwort lokal ab, wird nie gespeichert."""
    _, client = session_client()
    cmd_str = " ".join(args.cmd) if args.cmd else ""
    if not cmd_str:
        print("Fehler: Befehl angeben.", file=sys.stderr)
        return 1
    password = __import__("getpass").getpass(f"[sudo] Passwort: ")
    if not password:
        print("Abgebrochen.", file=sys.stderr)
        return 1
    # sudo -S liest von stdin; Passwort via echo pipen
    # Nutze shell-Tool: sudo -S liest Passwort von stdin
    full_cmd = "echo " + repr(password) + " | sudo -S " + cmd_str
    params: dict = {"command": full_cmd}
    print(f"sudo {cmd_str}", file=sys.stderr)
    with Spinner("working..."):
        try:
            raw = client.mcp_call("shell", params)
        except ClientError as e:
            print(f"Fehler: {e}", file=sys.stderr)
            return 1
    content = raw.get("result", {}).get("content", [{}])[0].get("text", "")
    try:
        data = json.loads(content)
        out = data.get("stdout", "") or data.get("output", "") or data.get("result","") or content
        err = (data.get("stderr", "") or "").replace(password, "***")
        rc = int(data.get("returncode", data.get("exit_code", 0)) or 0)
    except Exception:
        out, err, rc = content, "", 0
    if out:
        print(out)
    if err and not err.strip().startswith("[sudo]"):
        print(err, file=sys.stderr)
    return rc


def cmd_sysinfo(args: argparse.Namespace) -> int:
    """System-Uebersicht: lokal (--local) oder Backend via safe_probe."""
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
                cmds = {"cmd": [p]}
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

    # Remote: safe_probe via MCP
    _, client = session_client()
    action = getattr(args, "action", "overview")
    params: dict = {"action": action}
    probe = getattr(args, "probe", None)
    if probe:
        params["probe"] = probe
    service = getattr(args, "service", None)
    if service:
        params["service"] = service
    print("\033[2m(Backend-Server: Hetzner/ailinux — nicht lokaler Rechner)\033[0m", file=sys.stderr)
    with Spinner("working..."):
        try:
            raw = client.mcp_call("safe_probe", params)
        except ClientError as e:
            print(f"Fehler: {e}", file=sys.stderr)
            return 1
    content = raw.get("result", {}).get("content", [{}])[0].get("text", "")
    try:
        print_json(json.loads(content))
    except Exception:
        print(content)
    return 0


def cmd_service(args: argparse.Namespace) -> int:
    """Systemd-Service LOKAL verwalten (subprocess, nicht MCP)."""
    import subprocess as _sp
    action = args.action
    service = getattr(args, "service", None)

    if action == "list":
        r = _sp.run(["systemctl", "list-units", "--type=service",
                     "--state=running", "--no-pager", "--no-legend"],
                    capture_output=True, text=True, timeout=10)
        print(r.stdout.rstrip() or r.stderr.rstrip())
        return r.returncode

    if not service:
        print(f"Fehler: service angeben. Bsp: aicoder service {action} triforce",
              file=sys.stderr)
        return 1

    if action == "logs":
        n = getattr(args, "lines", 50)
        cmd = ["journalctl", "-u", service, f"-n{n}", "--no-pager"]
    else:
        cmd = ["systemctl", action, service]

    print(f"$ {' '.join(cmd)}", file=sys.stderr)
    r = _sp.run(cmd, capture_output=True, text=True, timeout=30)
    out = (r.stdout or r.stderr or "").rstrip()
    if out:
        print(out)
    return r.returncode


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
    p.add_argument("--timeout", type=int, default=90, help="HTTP-Timeout in Sekunden")
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
    p.add_argument("--timeout", type=int, default=90, help="HTTP-Timeout in Sekunden")
    p.set_defaults(func=cmd_task)

    p = sub.add_parser("review", help="Strukturiertes Code-Review einer Datei")
    p.add_argument("-f", "--file", dest="files", action="append", metavar="FILE")
    p.add_argument("--model", default=None)
    p.add_argument("--no-agents", dest="no_agents", action="store_true")
    p.set_defaults(func=cmd_review)

    # models / mcp-list
    p = sub.add_parser("models", help="Verfügbare Modelle vom Backend auflisten")
    p.add_argument("--filter", default=None, help="Filter by substring")
    p.add_argument("--group", action="store_true", help="Nach Provider gruppieren")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--json", dest="json_out", action="store_true")
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("mcp-list", help="Alle MCP-Tools tabellarisch anzeigen")
    p.set_defaults(func=cmd_mcp_list)

    # history
    p = sub.add_parser("hist", help="Call-History anzeigen")
    p.add_argument("-n", type=int, default=10)
    p.add_argument("--clear", action="store_true")
    p.set_defaults(func=cmd_hist)

    p = sub.add_parser("init", help="Workspace initialisieren + AGENTS.md anlegen")
    p.add_argument("path", nargs="?")
    p.add_argument("--force", action="store_true")
    p.add_argument("--no-git", dest="no_git", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("broadcast", help="Swarm-Broadcast an alle Backend-Modelle")
    p.add_argument("question", nargs="*")
    p.add_argument("--providers", default=None, help="Kommagetrennt: groq,mistral")
    p.add_argument("--skip", default=None)
    p.add_argument("--top-n", dest="top_n", type=int, default=5)
    p.add_argument("--max-tokens", dest="max_tokens", type=int, default=200)
    p.set_defaults(func=cmd_broadcast)

    p = sub.add_parser("shell", help="Befehl via MCP binary_exec ausfuehren (ohne Args: Liste)")
    p.add_argument("cmd", nargs="*")
    p.add_argument("--raw", "-r", action="store_true", help="Shell-Tool statt binary_exec (pipes etc.)")
    p.add_argument("--elevated", "-e", action="store_true")
    p.add_argument("--cwd", default=None)
    p.add_argument("--timeout", type=int, default=30)
    p.set_defaults(func=cmd_shell)

    p = sub.add_parser("sudo", help="Sudo-Befehl - fragt Passwort lokal ab")
    p.add_argument("cmd", nargs="*")
    p.add_argument("--timeout", type=int, default=60)
    p.set_defaults(func=cmd_sudo)

    p = sub.add_parser("sysinfo", help="System-Uebersicht: --local = dieser Rechner, sonst Backend-Server")
    p.add_argument("action", nargs="?", default="overview",
                   choices=["overview","run","service_status","journal","list"])
    p.add_argument("--probe", default=None)
    p.add_argument("--service", default=None)
    p.add_argument("--local", "-l", action="store_true", help="Lokale Stats (dieser Rechner, kein MCP)")
    p.set_defaults(func=cmd_sysinfo)

    p = sub.add_parser("service", help="Systemd-Service verwalten")
    p.add_argument("action", choices=["status","start","stop","restart","logs","list"])
    p.add_argument("service", nargs="?", default=None)
    p.add_argument("--lines", type=int, default=50)
    p.set_defaults(func=cmd_service)

    p = sub.add_parser("agent", help="Agent-REPL / autonomer Terminal-Agent")
    p.add_argument("prompt", nargs="*", help="Direkt-Prompt (kein REPL)")
    p.add_argument("--model", default=None)
    p.add_argument("--setup", action="store_true")
    p.add_argument("--verbose", "-v", action="store_true")
    p.set_defaults(func=cmd_agent)


    # GUI
    p = sub.add_parser("gui", help="GUI-Fenster starten (PyQt6)")
    p.set_defaults(func=lambda _: _run_gui())
    return parser


def cmd_agent(args: argparse.Namespace) -> int:
    """Agent-REPL starten (optional: direkter Prompt als Argument)."""
    from .setup import run_repl, run_setup
    from .agent import run_agent

    # --setup Flag: nur Wizard, dann REPL
    if getattr(args, "setup", False):
        run_setup(force=True)

    prompt_parts = getattr(args, "prompt", []) or []
    if prompt_parts:
        # Direkt-Prompt: kein REPL, einmaliger Agent-Run
        from .session_state import get_state
        state = get_state()
        return run_agent(
            initial_prompt=" ".join(prompt_parts),
            model=getattr(args, "model", None) or state.get("selected_model"),
            fallback_model=state.get("fallback_model"),
            verbose=getattr(args, "verbose", False),
        )
    else:
        return run_repl(skip_setup=getattr(args, "setup", False))

    # debug/demo
    p = sub.add_parser("status-demo", help="Nur Statusphasen lokal testen")
    p.add_argument("--mode", default="swarm")
    p.add_argument("--seconds", type=float, default=2.0)
    p.set_defaults(func=cmd_status_demo)

    # GUI
    p = sub.add_parser("gui", help="GUI-Fenster starten (PyQt6)")
    p.set_defaults(func=lambda _: _run_gui())

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
    p.add_argument("--group", action="store_true", help="Nach Provider gruppieren")
    p.add_argument("--verbose", "-v", action="store_true")
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

    p = sub.add_parser("init", help="Workspace initialisieren + AGENTS.md anlegen")
    p.add_argument("path", nargs="?")
    p.add_argument("--force", action="store_true")
    p.add_argument("--no-git", dest="no_git", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("broadcast", help="Swarm-Broadcast an alle Backend-Modelle")
    p.add_argument("question", nargs="*")
    p.add_argument("--providers", default=None, help="Kommagetrennt: groq,mistral")
    p.add_argument("--skip", default=None)
    p.add_argument("--top-n", dest="top_n", type=int, default=5)
    p.add_argument("--max-tokens", dest="max_tokens", type=int, default=200)
    p.set_defaults(func=cmd_broadcast)

    p = sub.add_parser("shell", help="Befehl via MCP binary_exec ausfuehren (ohne Args: Liste)")
    p.add_argument("cmd", nargs="*")
    p.add_argument("--raw", "-r", action="store_true", help="Shell-Tool statt binary_exec (pipes etc.)")
    p.add_argument("--elevated", "-e", action="store_true")
    p.add_argument("--cwd", default=None)
    p.add_argument("--timeout", type=int, default=30)
    p.set_defaults(func=cmd_shell)

    p = sub.add_parser("sudo", help="Sudo-Befehl - fragt Passwort lokal ab")
    p.add_argument("cmd", nargs="*")
    p.add_argument("--timeout", type=int, default=60)
    p.set_defaults(func=cmd_sudo)

    p = sub.add_parser("sysinfo", help="System-Uebersicht: --local = dieser Rechner, sonst Backend-Server")
    p.add_argument("action", nargs="?", default="overview",
                   choices=["overview","run","service_status","journal","list"])
    p.add_argument("--probe", default=None)
    p.add_argument("--service", default=None)
    p.add_argument("--local", "-l", action="store_true", help="Lokale Stats (dieser Rechner, kein MCP)")
    p.set_defaults(func=cmd_sysinfo)

    p = sub.add_parser("service", help="Systemd-Service verwalten")
    p.add_argument("action", choices=["status","start","stop","restart","logs","list"])
    p.add_argument("service", nargs="?", default=None)
    p.add_argument("--lines", type=int, default=50)
    p.set_defaults(func=cmd_service)


def cmd_agent(args: argparse.Namespace) -> int:
    """Agent-REPL starten (optional: direkter Prompt als Argument)."""
    from .setup import run_repl, run_setup
    from .agent import run_agent

    # --setup Flag: nur Wizard, dann REPL
    if getattr(args, "setup", False):
        run_setup(force=True)

    prompt_parts = getattr(args, "prompt", []) or []
    if prompt_parts:
        # Direkt-Prompt: kein REPL, einmaliger Agent-Run
        from .session_state import get_state
        state = get_state()
        return run_agent(
            initial_prompt=" ".join(prompt_parts),
            model=getattr(args, "model", None) or state.get("selected_model"),
            fallback_model=state.get("fallback_model"),
            verbose=getattr(args, "verbose", False),
        )
    else:
        return run_repl(skip_setup=getattr(args, "setup", False))

    # debug/demo
    p = sub.add_parser("status-demo", help="Nur Statusphasen lokal testen")
    p.add_argument("--mode", default="swarm")
    p.add_argument("--seconds", type=float, default=2.0)
    p.set_defaults(func=cmd_status_demo)

    # GUI
    p = sub.add_parser("gui", help="GUI-Fenster starten (PyQt6)")
    p.set_defaults(func=lambda _: _run_gui())

    return parser


def _run_gui() -> int:
    """Startet die PyQt6 GUI."""
    try:
        from .gui.app import run_gui
        return run_gui()
    except ImportError as e:
        print(f"PyQt6 nicht installiert: {e}", file=sys.stderr)
        print("Installation: pip install PyQt6", file=sys.stderr)
        return 1


def main() -> int:
    # Kein Argument → Setup-Wizard + Agent-REPL starten
    if len(sys.argv) == 1:
        from .setup import run_repl
        return run_repl()

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
