"""Small stdio MCP adapter for trusted AICoder ToolProviders."""
from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from .plugins import ToolProvider, discover_plugins

MODERN_PROTOCOL = "2026-07-28"
LEGACY_PROTOCOL = "2025-11-25"


def _tool_schema(tool) -> dict[str, Any]:
    schema=dict(tool.schema)
    schema["annotations"]={
        **(schema.get("annotations") if isinstance(schema.get("annotations"),dict) else {}),
        "readOnlyHint": bool(tool.security.read_only),
        "destructiveHint": bool(tool.security.destructive),
    }
    return schema


def _read_only_tools(provider: ToolProvider) -> tuple:
    return tuple(tool for tool in provider.tools() if tool.security.read_only and not tool.security.mutating)


def _response(request_id: Any, *, result: Any = None, error: dict | None = None) -> dict[str, Any]:
    payload={"jsonrpc":"2.0","id":request_id}
    if error is not None: payload["error"]=error
    else: payload["result"]=result
    return payload


def handle_request(message: dict[str, Any], provider: ToolProvider) -> dict[str, Any] | None:
    method=str(message.get("method") or "")
    request_id=message.get("id")
    if request_id is None and method.startswith("notifications/"):
        return None
    if method == "server/discover":
        return _response(request_id, result={
            "supportedVersions":[MODERN_PROTOCOL,LEGACY_PROTOCOL],
            "capabilities":{"tools":{"listChanged":False}},
            "_meta":{"io.modelcontextprotocol/serverInfo":{"name":"aicoder-local-os","version":"1.0.0","description":"AICoder read-only Local OS provider"}},
        })
    if method == "initialize":
        params=message.get("params") if isinstance(message.get("params"),dict) else {}
        requested=str(params.get("protocolVersion") or LEGACY_PROTOCOL)
        version=requested if requested != MODERN_PROTOCOL else LEGACY_PROTOCOL
        return _response(request_id, result={
            "protocolVersion":version,
            "capabilities":{"tools":{"listChanged":False}},
            "serverInfo":{"name":"aicoder-local-os","version":"1.0.0"},
        })
    if method == "tools/list":
        return _response(request_id, result={
            "tools":[_tool_schema(tool) for tool in _read_only_tools(provider)],
            "ttlMs":300000,
            "cacheScope":"private",
        })
    if method == "tools/call":
        params=message.get("params") if isinstance(message.get("params"),dict) else {}
        name=str(params.get("name") or "")
        args=params.get("arguments") if isinstance(params.get("arguments"),dict) else {}
        definitions={tool.name:tool for tool in provider.tools()}
        tool=definitions.get(name)
        if tool is None:
            return _response(request_id, error={"code":-32602,"message":f"unknown tool: {name}"})
        if not tool.security.read_only or tool.security.mutating:
            text="Mutating Local OS tools are not exposed over external stdio MCP; use native AICoder approval flow."
            return _response(request_id, result={"content":[{"type":"text","text":text}],"isError":True})
        text,is_error=provider.execute(name,args)
        return _response(request_id, result={"content":[{"type":"text","text":text}],"isError":bool(is_error)})
    return _response(request_id, error={"code":-32601,"message":f"method not found: {method}"})


def serve_stdio(provider: ToolProvider, *, stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    input_stream=stdin or sys.stdin
    output_stream=stdout or sys.stdout
    for raw in input_stream:
        line=raw.strip()
        if not line: continue
        try:
            message=json.loads(line)
            if not isinstance(message,dict): raise ValueError("JSON-RPC message must be an object")
            response=handle_request(message,provider)
        except Exception as exc:
            response=_response(None,error={"code":-32700,"message":f"parse/dispatch error: {exc}"})
        if response is not None:
            output_stream.write(json.dumps(response,ensure_ascii=False,separators=(",",":"))+"\n")
            output_stream.flush()
    return 0


def serve_plugin_stdio(plugin_id: str, workspace_root: str) -> int:
    registry=discover_plugins(workspace_root)
    record=registry.get(plugin_id)
    if record is None or not record.enabled or not record.executable or record.provider is None:
        print(f"MCP provider unavailable: {plugin_id}",file=sys.stderr)
        return 2
    return serve_stdio(record.provider)
