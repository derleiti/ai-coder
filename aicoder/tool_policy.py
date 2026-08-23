"""Central capability policy for local and remote ai-coder tools.

The backend account/RBAC and the advertised MCP catalogue define which remote
capabilities exist. The desktop client does not impose a second task-category
namespace filter. Local risk, workspace and privilege policy is enforced at the
execution boundary instead.
"""
from __future__ import annotations

import re
from collections.abc import Iterable


# Explicit client workflows which are not ordinary operator-facing tools.
INTERNAL_MCP_TOOLS = {"swarm_broadcast"}

# These names are implemented by the AICoder runtime itself. They must never be
# dispatched to the remote TriForce MCP transport, even if a backend happens to
# advertise an identically named tool.
LOCAL_ONLY_TOOLS = frozenset({"shell", "binary_exec", "task_runner"})

# TriForce is a backend service, never an operator target. These MCP tools expose
# the backend host, its repository, local process/container/service state, or
# remote-node administration. AICoder may have identically named LOCAL tools,
# but must never dispatch these capabilities over the TriForce MCP transport.
TRIFORCE_HOST_MCP_TOOLS = frozenset({
    "code_read", "code_tree", "code_search", "code_edit", "code_patch", "code_grep",
    "dev_analyze", "dev_debug", "dev_links", "dev_lint", "dev_refactor", "dev_summarize", "devops",
    "file_ops", "file_read", "file_write", "file_edit", "file_tree", "directory_create",
    "git", "git_ops", "custom_exec", "custom_binary", "binary_exec", "task_runner", "shell",
    "package_manager", "service_control", "service_status", "container_control", "container_status",
    "remote_task", "remote_admin", "remote_status", "mesh_task", "safe_probe",
    "agent_start", "agent_stop", "agent_review", "agent_broadcast", "restart",
    "template_list", "task_reference", "binary_list",
})


def triforce_host_forbidden_reason(name: str) -> str | None:
    """Return a reason when an MCP call would treat TriForce as an operator target."""
    canonical = canonical_tool_name(name)
    if canonical in TRIFORCE_HOST_MCP_TOOLS:
        return (
            f"tool '{name}' is blocked over TriForce MCP: TriForce is a backend service, "
            "not a remotely administrable AICoder target"
        )
    return None

# None means: trust the authenticated backend catalogue instead of maintaining
# a stale duplicate allowlist in the client. Kept under the historical name for
# import compatibility while callers migrate to OPERATOR_MCP_TOOLS.
OPERATOR_MCP_TOOLS: frozenset[str] | None = None
CODING_MCP_TOOLS = OPERATOR_MCP_TOOLS


def canonical_tool_name(name: str) -> str:
    """Return the non-namespaced leaf used for policy classification."""
    normalized = str(name or "").strip().lower()
    return re.split(r"[./:]", normalized)[-1]


def forbidden_reason(name: str) -> str | None:
    """Legacy compatibility hook.

    Tool categories such as admin/devops/service/remote are no longer denied by
    name. Authorization comes from backend RBAC/catalogue and execution risk is
    handled by the local approval/PrivilegeBroker layer.
    """
    return None


def require_allowed_tool(
    name: str,
    allowed_names: Iterable[str] | None,
    *,
    allow_internal: bool = False,
) -> tuple[bool, str]:
    """Validate a tool against transport invariants and an optional run subset."""
    canonical = canonical_tool_name(name)
    if not canonical:
        return False, "tool name is empty"

    if canonical in INTERNAL_MCP_TOOLS:
        if allow_internal:
            return True, ""
        return False, f"tool '{name}' is reserved for an explicit internal client workflow"

    if allowed_names is None:
        return True, ""

    allowed = {str(item) for item in allowed_names}
    allowed_canonical = {canonical_tool_name(item) for item in allowed}
    if name not in allowed and canonical not in allowed_canonical:
        return False, f"tool '{name}' is not enabled for this run"
    return True, ""


def filter_tool_catalog(
    tools: Iterable[dict],
    allowed_names: Iterable[str] | None = None,
) -> list[dict]:
    """Return well-formed schemas from the authenticated backend catalogue.

    If *allowed_names* is supplied it is a per-run capability subset, not a
    global admin/ops denylist. Local-only runtime names are never accepted from
    MCP because their execution boundary is intentionally local.
    """
    allowed_canonical = (
        {canonical_tool_name(item) for item in allowed_names}
        if allowed_names is not None else None
    )
    raw_tools = list(tools)
    advertised_canonical = {
        canonical_tool_name(t.get("name"))
        for t in raw_tools if isinstance(t, dict) and isinstance(t.get("name"), str)
    }
    result: list[dict] = []
    for tool in raw_tools:
        if not isinstance(tool, dict):
            continue
        normalized = dict(tool)
        if isinstance(tool.get("function"), dict):
            function = tool["function"]
            normalized = {
                "name": function.get("name"),
                "description": function.get("description", ""),
                "inputSchema": function.get("parameters", {}),
                **({"annotations": tool["annotations"]} if isinstance(tool.get("annotations"), dict) else {}),
            }
        if not isinstance(normalized.get("inputSchema"), dict):
            candidate = normalized.get("input_schema") or normalized.get("parameters")
            normalized["inputSchema"] = candidate if isinstance(candidate, dict) else {
                "type": "object", "properties": {},
            }
        name = normalized.get("name")
        if not isinstance(name, str) or not canonical_tool_name(name):
            continue
        canonical = canonical_tool_name(name)
        if canonical in LOCAL_ONLY_TOOLS:
            continue
        if triforce_host_forbidden_reason(name):
            continue
        # Keep the canonical unified search surface when a backend also
        # advertises a legacy alias. This is schema hygiene, not a permission
        # restriction; the operator still receives the full capability.
        if canonical == "web_search" and "search" in advertised_canonical:
            continue
        if allowed_canonical is not None and canonical not in allowed_canonical:
            continue
        ok, _ = require_allowed_tool(name, allowed_names)
        if ok:
            result.append(normalized)
    return result
