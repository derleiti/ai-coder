from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from aicoder import executor


def _semantic(tool: dict) -> str:
    return json.dumps(
        {"name": tool["name"], "inputSchema": tool.get("inputSchema") or {}},
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_sample() -> list[dict]:
    return [
        {
            "name": "shell",
            "description": "canonical shell",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string"},
                    "sudo": {"type": "boolean"},
                    "timeout": {"type": "integer"},
                },
                "required": ["command"],
            },
            "annotations": {"readOnlyHint": False},
        },
        {
            "name": "git",
            "description": "canonical git",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["status", "diff", "commit", "branch", "log", "push", "pull", "stash", "add"]},
                    "message": {"type": "string"},
                    "branch": {"type": "string"},
                    "path": {"type": "string"},
                    "args": {"type": "string"},
                },
                "required": ["mode"],
            },
        },
        {
            "name": "code_search",
            "description": "canonical search",
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string"}, "path": {"type": "string"}},
                "required": ["query"],
            },
        },
        {
            "name": "code_tree",
            "description": "canonical tree",
            "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    ]


def test_local_shared_names_project_exact_canonical_semantic_schema():
    canonical = _canonical_sample()
    projected = {tool["name"]: tool for tool in executor.project_local_tool_schemas(canonical)}
    canonical_by_name = {tool["name"]: tool for tool in canonical}
    for name in canonical_by_name:
        assert _semantic(projected[name]) == _semantic(canonical_by_name[name])
        assert projected[name]["x_execution"] == "local_aicoder"
    assert projected["file_read"]["inputSchema"] == executor.LOCAL_FILE_READ_SCHEMA["inputSchema"]


def test_load_tools_rebinds_canonical_schema_to_local_handler():
    canonical = _canonical_sample()
    client = MagicMock()
    client.base_url = "https://example.invalid"
    client.token = "opaque"
    client._request.return_value = {"result": {"tools": canonical}}
    registry = MagicMock()
    registry.tool_schemas.return_value = []
    with (
        patch.object(executor, "_tool_cache", None),
        patch.object(executor, "_tool_cache_ts", 0),
        patch.object(executor, "_tool_cache_key", None),
        patch.object(executor, "_tool_security_hints", {}),
        patch.object(executor, "discover_plugins", return_value=registry),
        patch("aicoder.mcp_registry.external_tool_schemas", return_value=[]),
    ):
        loaded = {tool["name"]: tool for tool in executor.load_tools(client, force_refresh=True)}
    for tool in canonical:
        name = tool["name"]
        assert _semantic(loaded[name]) == _semantic(tool)
        assert loaded[name]["x_execution"] == "local_aicoder"


def test_canonical_git_status_shape_executes_locally(tmp_path: Path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    with patch.object(executor, "_RUNTIME_WORKSPACE_ROOT") as runtime_root:
        runtime_root.get.return_value = str(tmp_path)
        result, is_error = executor.run_git_read({"mode": "status", "path": str(tmp_path), "args": ""})
    assert not is_error, result
    assert "##" in result or result == "(no output)"


def test_canonical_git_mutation_is_blocked_without_approval(tmp_path: Path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "new.txt").write_text("new\n")
    client = MagicMock()
    with patch.object(executor.audit, "log_tool"):
        result, is_error = executor.run_tool(
            client,
            "git",
            {"mode": "add", "path": str(tmp_path), "args": "new.txt"},
            workspace_root=tmp_path,
        )
    assert is_error is True
    assert "blocked" in result
