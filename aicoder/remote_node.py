"""Remote coding node for the internal native-light preview.

The node connects to TriForce's existing /v1/mcp/node WebSocket. Read-only
workspace tools are always available. A deliberately small write surface can be
opted into locally with ``aicoder remote-node --allow-writes``.

Remote writes are restricted to file creation and exact text replacement. There
is no delete, arbitrary overwrite, append, shell execution, clipboard access or
unrestricted MCP forwarding. Existing files are backed up outside the workspace
before mutation so the remote model cannot rewrite its own rollback copy.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import shutil
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

from . import __version__
from .agent_plan import PlanStore
from .config import CONFIG_DIR, Session, load_session
from .executor import (
    _workspace_path,
    _workspace_root,
    run_code_grep,
    run_file_edit,
    run_file_read,
    run_file_tree,
    run_git_read,
)
from .session_state import get_state
from .workspace import active_workspace

REMOTE_READ_TOOLS = {
    "client_file_read",
    "client_file_list",
    "client_codebase_search",
    "client_git_status",
}
REMOTE_WRITE_TOOLS = {"client_file_edit"}
REMOTE_CONTROL_TOOLS = {"client_run_state"}
REMOTE_MODEL_TOOLS = REMOTE_READ_TOOLS | REMOTE_WRITE_TOOLS
REMOTE_TOOLS = REMOTE_MODEL_TOOLS | REMOTE_CONTROL_TOOLS
REMOTE_WRITE_OPERATIONS = {"create", "replace"}
REMOTE_PLAN_RUNTIME = "remote-antigravity-light"


def websocket_node_url(base_url: str, session: Session) -> str:
    """Build the authenticated MCP-node URL from the configured TriForce URL."""
    parsed = urlsplit(base_url.rstrip("/"))
    if parsed.scheme not in {"http", "https", "ws", "wss"}:
        raise ValueError(f"unsupported TriForce URL scheme: {parsed.scheme}")
    scheme = "wss" if parsed.scheme in {"https", "wss"} else "ws"
    base_path = parsed.path.rstrip("/")
    path = f"{base_path}/v1/mcp/node/connect"
    query = urlencode({
        "token": session.token,
        "session_id": session.client_id or "aicoder",
        "machine_id": socket.gethostname(),
        "user_id": session.user_id,
        "tier": session.tier,
        "client_version": __version__,
        "mode": "full",
    })
    return urlunsplit((scheme, parsed.netloc, path, query, ""))



def _remote_plan_store() -> PlanStore:
    return PlanStore(CONFIG_DIR / "remote-plans")


def _remote_plan_for_call(arguments: dict[str, Any], *, create: bool = False):
    run_id = str(arguments.get("_run_id") or "").strip()
    if not run_id:
        return None
    store = _remote_plan_store()
    workspace = str(_workspace_root())
    plan = store.load(workspace, run_id)
    if plan is None and create:
        task = str(arguments.get("_task") or "remote coding task").strip() or "remote coding task"
        plan = store.create(
            task,
            workspace,
            str(arguments.get("_model") or ""),
            plan_id=run_id,
            runtime=REMOTE_PLAN_RUNTIME,
        )
        plan.record_event("remote_start", "Remote Antigravity run started")
        store.save(plan)
    return plan


def _update_remote_plan(
    arguments: dict[str, Any],
    *,
    tool: str,
    is_error: bool,
    mutation: bool = False,
    verification: bool = False,
) -> None:
    run_id = str(arguments.get("_run_id") or "").strip()
    if not run_id:
        return
    store = _remote_plan_store()
    workspace = str(_workspace_root())
    plan = store.load(workspace, run_id)
    if plan is None:
        plan = _remote_plan_for_call(arguments, create=True)
    if plan is None:
        return
    plan.status = "running"
    plan.record_event("remote_tool", f"{tool} {'failed' if is_error else 'completed'}", tool=tool, is_error=is_error)
    if not is_error:
        if mutation:
            plan.set_step("inspect", "completed", "Remote workspace inspected before mutation")
            plan.set_step("implement", "completed", f"Remote mutation via {tool}")
            plan.set_step("verify", "in_progress", "Waiting for remote post-change verification")
        elif verification:
            implement = next((step for step in plan.steps if step.id == "implement"), None)
            if implement is not None and implement.status == "completed":
                plan.set_step("verify", "completed", f"Remote verification via {tool}")
            else:
                plan.set_step("inspect", "completed", f"Remote inspection via {tool}")
    store.save(plan)


def finalize_remote_plan(arguments: dict[str, Any], *, status: str, response: str = "", reason: str = "") -> None:
    run_id = str(arguments.get("_run_id") or "").strip()
    if not run_id:
        return
    store = _remote_plan_store()
    workspace = str(_workspace_root())
    plan = store.load(workspace, run_id)
    if plan is None:
        return
    plan.status = status
    plan.last_response = str(response or "")[:4000]
    plan.pause_reason = str(reason or "")[:1000]
    plan.record_event("remote_complete" if status == "completed" else "remote_pause", reason or status, is_error=status == "failed")
    store.save(plan)

def _tool_text(text: str, is_error: bool) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": str(text)}],
        "isError": bool(is_error),
    }


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _backup_existing_remote_file(path: Path) -> Path:
    """Copy an existing workspace file to a model-inaccessible rollback area."""
    root = _workspace_root()
    relative = path.relative_to(root)
    workspace_key = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_root = CONFIG_DIR / "backups" / "remote" / workspace_key / stamp
    backup_path = backup_root / relative
    _private_dir(backup_path.parent)
    shutil.copy2(path, backup_path)
    try:
        os.chmod(backup_path, 0o600)
    except OSError:
        pass
    return backup_path


def _execute_remote_write(arguments: dict[str, Any]) -> dict[str, Any]:
    args = dict(arguments or {})
    operation = str(args.get("operation") or "").strip().lower()
    if operation not in REMOTE_WRITE_OPERATIONS:
        return _tool_text(
            "remote write preview allows only operation=create or operation=replace",
            True,
        )
    path_value = args.get("path")
    if not isinstance(path_value, str) or not path_value.strip():
        return _tool_text("remote file edit requires path", True)

    try:
        path = _workspace_path(path_value, must_exist=False)
    except Exception as exc:
        return _tool_text(f"remote file edit blocked: {exc}", True)

    backup_path: Path | None = None
    if operation == "replace":
        if not path.exists() or not path.is_file():
            return _tool_text(f"remote replace requires an existing regular file: {path_value}", True)
        old_text = args.get("old_text")
        new_text = args.get("new_text")
        if not isinstance(old_text, str) or not old_text or not isinstance(new_text, str):
            return _tool_text("remote replace requires non-empty old_text and string new_text", True)
        try:
            current = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            return _tool_text(f"remote replace could not read target: {exc}", True)
        count = current.count(old_text)
        if count != 1:
            return _tool_text(
                f"remote replace requires old_text to match exactly once (matched {count})",
                True,
            )
        try:
            backup_path = _backup_existing_remote_file(path)
        except OSError as exc:
            return _tool_text(f"remote replace refused because backup failed: {exc}", True)
        edit_args = {
            "path": path_value,
            "operation": "replace",
            "old_text": old_text,
            "new_text": new_text,
        }
    else:
        if path.exists():
            return _tool_text(f"remote create refuses existing path: {path_value}", True)
        content = args.get("content")
        if not isinstance(content, str):
            return _tool_text("remote create requires string content", True)
        edit_args = {"path": path_value, "operation": "create", "content": content}

    result, is_error = run_file_edit(edit_args)
    if is_error:
        return _tool_text(result, True)
    if backup_path is not None:
        result += f"\nbackup={backup_path}"
    else:
        result += "\nbackup=none (new file)"
    result += "\nverification_required=true"
    return _tool_text(result, False)


def execute_remote_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    allow_writes: bool = False,
) -> dict[str, Any]:
    """Execute one allowlisted remote operation inside the active workspace."""
    args = dict(arguments or {})
    metadata_args = dict(args)
    for key in ("_run_id", "_task", "_model"):
        args.pop(key, None)
    if name == "client_run_state":
        status = str(args.get("status") or "").strip().lower()
        if status not in {"completed", "paused", "failed"}:
            return _tool_text("client_run_state requires completed, paused, or failed status", True)
        finalize_remote_plan(
            metadata_args,
            status=status,
            response=str(args.get("response") or ""),
            reason=str(args.get("reason") or ""),
        )
        return _tool_text(f"remote run {status}", False)
    if name in REMOTE_WRITE_TOOLS:
        if not allow_writes:
            result = _tool_text(f"remote read-only profile blocks tool: {name}", True)
            _update_remote_plan(metadata_args, tool=name, is_error=True)
            return result
        result = _execute_remote_write(args)
        _update_remote_plan(metadata_args, tool=name, is_error=bool(result.get("isError")), mutation=not bool(result.get("isError")))
        return result
    if name == "client_file_read":
        result, is_error = run_file_read({
            "path": args.get("path"),
            "start_line": args.get("start_line"),
            "end_line": args.get("end_line"),
        })
    elif name == "client_file_list":
        result, is_error = run_file_tree({
            "path": args.get("path") or ".",
            "max_depth": 5 if bool(args.get("recursive")) else 1,
            "max_entries": args.get("max_entries") or 300,
        })
    elif name == "client_codebase_search":
        result, is_error = run_code_grep({
            "pattern": args.get("query") or args.get("pattern"),
            "path": args.get("path") or ".",
            "glob": args.get("file_pattern") or args.get("glob") or "*",
            "max_results": args.get("max_results") or 200,
        })
    elif name == "client_git_status":
        result, is_error = run_git_read({
            "action": "status",
            "cwd": args.get("path") or ".",
            "args": ["--short", "--branch"],
        })
    else:
        blocked = _tool_text(f"remote tool blocked: {name}", True)
        _update_remote_plan(metadata_args, tool=name, is_error=True)
        return blocked
    wrapped = _tool_text(result, is_error)
    _update_remote_plan(
        metadata_args,
        tool=name,
        is_error=bool(is_error),
        verification=(name in {"client_file_read", "client_git_status"}),
    )
    return wrapped


def execute_remote_read_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Backwards-compatible read-only wrapper used by existing callers/tests."""
    return execute_remote_tool(name, arguments, allow_writes=False)


@dataclass
class RemoteNode:
    session: Session
    base_url: str
    allow_writes: bool = False

    @property
    def advertised_tools(self) -> set[str]:
        if self.allow_writes:
            return REMOTE_MODEL_TOOLS | REMOTE_CONTROL_TOOLS
        return REMOTE_READ_TOOLS | REMOTE_CONTROL_TOOLS

    @property
    def remote_profile(self) -> str:
        return "write-preview" if self.allow_writes else "read-only-light"

    async def _send_identity(self, websocket: Any) -> None:
        state = get_state()
        await websocket.send(json.dumps({
            "jsonrpc": "2.0",
            "method": "client/info",
            "params": {
                "platform": platform.system().lower(),
                "hostname": socket.gethostname(),
                "server_version": __version__,
                "client": "aicoder",
                "mode": "full",
                "workspace": str(active_workspace(state.get("workspace_root"))),
                "remote_profile": self.remote_profile,
            },
        }))
        await websocket.send(json.dumps({
            "jsonrpc": "2.0",
            "method": "tools/list",
            "params": {"tools": sorted(self.advertised_tools)},
        }))

    async def _heartbeat(self, websocket: Any) -> None:
        while True:
            await asyncio.sleep(25)
            await websocket.send(json.dumps({"jsonrpc": "2.0", "method": "ping"}))

    async def _handle(self, websocket: Any, message: dict[str, Any]) -> None:
        if message.get("method") == "connected":
            await self._send_identity(websocket)
            return
        if message.get("method") != "tools/call":
            return
        request_id = message.get("id")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        name = str(params.get("name") or "")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        try:
            if name not in self.advertised_tools:
                result = _tool_text(f"remote profile blocks tool: {name}", True)
            else:
                result = await asyncio.to_thread(
                    execute_remote_tool,
                    name,
                    arguments,
                    allow_writes=self.allow_writes,
                )
            response = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception as exc:
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": f"AICoder remote tool failed: {exc}"},
            }
        await websocket.send(json.dumps(response, ensure_ascii=False))

    async def run(self) -> None:
        state = get_state()
        workspace = str(active_workspace(state.get("workspace_root")))
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError("remote-node requires the 'websockets' package") from exc

        url = websocket_node_url(self.base_url, self.session)
        async with websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
            max_size=2 * 1024 * 1024,
        ) as websocket:
            heartbeat = asyncio.create_task(self._heartbeat(websocket))
            try:
                async for raw in websocket:
                    try:
                        message = json.loads(raw)
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if isinstance(message, dict):
                        await self._handle(websocket, message)
            finally:
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass


def run_remote_node(*, allow_writes: bool = False) -> None:
    session = load_session()
    asyncio.run(
        RemoteNode(
            session=session,
            base_url=session.base_url,
            allow_writes=allow_writes,
        ).run()
    )
