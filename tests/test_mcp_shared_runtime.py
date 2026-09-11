from pathlib import Path
from aicoder.mcp_registry import MCPRegistry
import aicoder.mcp_service as service
import aicoder.shared_notify as shared


def test_shared_tools_join_runtime_catalog(monkeypatch, tmp_path):
    registry = MCPRegistry(tmp_path / "registry.json")
    monkeypatch.setattr(service, "_shared_mcp_endpoints", lambda: [{"endpoint_id":"ep_remote","handle":"@mcp-zombie-gimp","online":True}])
    monkeypatch.setattr(shared, "shared_mcp_tools", lambda handle, timeout=3.0: [{"name":"layer_create","description":"Create layer","inputSchema":{"type":"object"},"annotations":{"readOnlyHint":True}}])
    tools = service.external_tool_schemas(registry)
    assert len(tools) == 1
    assert tools[0]["name"] == "mcp.shared-mcp-zombie-gimp.layer_create"
    assert tools[0]["annotations"]["readOnlyHint"] is False


def test_shared_tool_call_routes_to_notify(monkeypatch, tmp_path):
    registry = MCPRegistry(tmp_path / "registry.json")
    monkeypatch.setattr(service, "_shared_mcp_endpoints", lambda: [{"endpoint_id":"ep_remote","handle":"@mcp-zombie-gimp","online":True}])
    seen = {}
    def call(handle, tool, args, timeout=30.0):
        seen.update(handle=handle, tool=tool, args=args)
        return "ok", False
    monkeypatch.setattr(shared, "call_shared_mcp_tool", call)
    result = service.call_external_tool("mcp.shared-mcp-zombie-gimp.layer_create", {"name":"BG"}, registry)
    assert result == ("ok", False)
    assert seen == {"handle":"@mcp-zombie-gimp", "tool":"layer_create", "args":{"name":"BG"}}
