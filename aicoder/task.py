from __future__ import annotations
"""
task.py — File-aware coding task runner for ai-coder.

Flow:
  1. Read target file(s)
  2. Build prompt: task description + file content
  3. LLM call via /v1/client/chat
  4. Show response (diff if --apply mode)
  5. Optionally write patch back to file
"""
import difflib
import os
import sys
from pathlib import Path
from typing import Optional

from .client import ClientError, TriForceClient
from .config import load_session
from .docs_context import read_agents_md
from .session_state import get_state
from .history import record as history_record
from .status import Spinner, phase_label

TASK_SYSTEM_SUFFIX = """
You are ai-coder in task mode — precise code modification on AILinux/TriForce.

INIT: current_time (check date) → memory_search (known patterns?) → then execute.

Rules:
- Return ONLY the complete modified file, no markdown fences, no explanation.
- Analysis tasks: plain text, concise, actionable.
- Never truncate. Return full file.
- If unsure about API/version: search first, never guess.
- Smallest change that solves the task.
"""


def _read_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception as e:
        raise RuntimeError(f"Datei nicht lesbar: {path}: {e}")


def _build_prompt(task: str, files: list[tuple[str, str]], context: str) -> str:
    parts = []
    if context:
        parts.append(f"Project context:\n{context[:2000]}")
    parts.append(f"Task: {task}")
    for name, content in files:
        parts.append(f"\n--- FILE: {name} ---\n{content}\n--- END: {name} ---")
    return "\n\n".join(parts)


def _show_diff(original: str, modified: str, filename: str) -> None:
    diff = list(difflib.unified_diff(
        original.splitlines(keepends=True),
        modified.splitlines(keepends=True),
        fromfile=f"a/{filename}",
        tofile=f"b/{filename}",
    ))
    if not diff:
        print("(no changes)")
        return
    for line in diff:
        if line.startswith("+") and not line.startswith("+++"):
            print(f"\033[32m{line}\033[0m", end="")
        elif line.startswith("-") and not line.startswith("---"):
            print(f"\033[31m{line}\033[0m", end="")
        else:
            print(line, end="")


def run_task(
    task: str,
    file_paths: list[str],
    model: Optional[str],
    apply: bool = False,
    dry_run: bool = False,
    no_agents: bool = False,
    temperature: float = 0.3,
) -> int:
    session = load_session()
    client = TriForceClient(session.base_url, token=session.token)
    state = get_state()

    effective_model = model or state.get("selected_model") or None
    swarm = state.get("swarm_mode", "off")
    workspace = state.get("workspace_root")

    # System prompt: AGENTS.md + task instructions
    agents_content = "" if no_agents else (read_agents_md(workspace) or "")
    system_prompt = (agents_content + "\n\n" + TASK_SYSTEM_SUFFIX).strip()

    # Read files
    files_content: list[tuple[str, str]] = []
    for fp in file_paths:
        p = Path(fp).resolve()
        if not p.exists():
            print(f"WARNUNG: Datei nicht gefunden: {fp}", file=sys.stderr)
            continue
        content = _read_file(p)
        files_content.append((str(p), content))

    if not files_content and file_paths:
        print("Fehler: Keine lesbaren Dateien.", file=sys.stderr)
        return 1

    # Project context (short workspace summary)
    context = ""
    if workspace:
        try:
            ctx_path = Path(workspace) / "AGENTS.md"
            if ctx_path.exists():
                context = ctx_path.read_text(encoding="utf-8")[:1500]
        except Exception:
            pass

    prompt = _build_prompt(task, files_content, context)

    print(f"model={effective_model or '(backend default)'}  files={len(files_content)}  apply={apply}", file=sys.stderr)

    fallback_model = state.get("fallback_model") or None
    # Auto-Swarm Heuristik
    _effective_swarm = swarm
    if swarm == "auto":
        from .swarm_runner import should_auto_swarm
        if should_auto_swarm(task):
            _effective_swarm = "on"
            print("swarm: auto-triggered", file=sys.stderr)

    label = phase_label(_effective_swarm if _effective_swarm != "off" else "work")
    with Spinner(label):
        result = client.chat(
            message=prompt,
            model=effective_model,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=8192,
            fallback_model=fallback_model,
        )

    response = result.get("response", "").strip()
    model_used = result.get("model", effective_model or "?")
    latency = result.get("latency_ms")

    # History
    try:
        history_record(
            kind="task", prompt=task,
            response=response,
            model=model_used,
            files=file_paths,
            latency_ms=latency,
        )
    except Exception:
        pass

    # Single-file: show diff + optional apply
    if len(files_content) == 1 and apply:
        orig_path_str, original = files_content[0]
        orig_path = Path(orig_path_str)

        print(f"\n── diff: {orig_path.name} ──────────────────────────")
        _show_diff(original, response, orig_path.name)
        print("─────────────────────────────────────────────────\n")

        if dry_run:
            print("(dry-run — nicht geschrieben)")
        else:
            confirm = input("Änderungen schreiben? [y/N] ").strip().lower()
            if confirm == "y":
                orig_path.write_text(response, encoding="utf-8")
                print(f"✓ {orig_path} aktualisiert")
            else:
                print("Abgebrochen.")
    elif len(files_content) > 1 and apply:
        # Multi-file apply via FILE-block parsing
        print()
        _apply_multifile(files_content, response, dry_run)
        print()
    else:
        # Analysis / read-only
        print()
        print(response)
        print()

    print(f"[{model_used} · {latency or '?'}ms]", file=sys.stderr)

    # Swarm Review: Fallback bewertet den Output
    if _effective_swarm == "review" and not apply:
        from .swarm_runner import run_swarm_review
        run_swarm_review(
            original_task=task,
            operator_response=response,
            operator_model=effective_model,
            fallback_model=state.get("fallback_model"),
            system_prompt=system_prompt if not no_agents else None,
        )

    return 0


def _apply_multifile(
    files_content: list,
    response: str,
    dry_run: bool,
) -> None:
    """Parse FILE-blocks from LLM response and apply per-file with diff+confirm."""
    import re
    pattern = re.compile(
        r"--- FILE: (.+?) ---\n(.*?)\n--- END: .+? ---",
        re.DOTALL,
    )
    blocks = pattern.findall(response)
    if not blocks:
        print(response)
        return
    for fname, content in blocks:
        fname = fname.strip()
        new_content = content.strip()
        # Match original path — prefer exact, then suffix (sep-safe)
        match_pair = next(
            ((n, c) for n, c in files_content
             if n == fname or n.endswith(os.sep + fname) or n.endswith("/" + fname)),
            None,
        )
        if match_pair is None:
            print(f"WARN: {fname} nicht in Eingabedateien — übersprungen")
            continue
        orig_path_str, match_content = match_pair
        # Write to original resolved path, never to LLM-supplied fname
        p = Path(orig_path_str)
        print(f"\n── diff: {p.name} ──────────────────────────────")
        _show_diff(match_content, new_content, p.name)
        if dry_run:
            print("(dry-run)")
            continue
        confirm = input(f"Schreiben? {p.name} [y/N] ").strip().lower()
        if confirm == "y":
            p.write_text(new_content, encoding="utf-8")
            print(f"✓ {p} aktualisiert")
        else:
            print("Übersprungen.")
