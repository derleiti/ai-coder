"""Central capability policy for local and remote ai-coder tools.

The backend is not a security boundary for the desktop client: every execution
entry point must apply this policy before a model supplied name is dispatched.
"""
from __future__ import annotations

import re
from collections.abc import Iterable


FORBIDDEN_PREFIXES = (
    "admin_",
    "vault_",
    "mail_",
    "notify_",
    "restart_",
    "service_",
    "remote_",
)

# Equivalent aliases are included even when AGENTS.md names only the historical
# MCP tool.  Renaming a remote shell must not bypass the coding-client scope.
FORBIDDEN_EXACT = {
    "shell",
    "task_runner",
    "binary_exec",
    "custom_exec",
    "local_exec",
    "devops",
    "service_control",
    "container_control",
    "remote_task",
    "mesh_task",
    "restart",
}

# Explicit client features which are safe to call directly but are never
# advertised to an operator model as executable project tools.
INTERNAL_MCP_TOOLS = {"swarm_broadcast"}

# Single source of truth for MCP capabilities which an operator model or direct
# coding-client command may invoke. Local capabilities live in executor.py.
CODING_MCP_TOOLS = frozenset({
    "code_read", "code_search", "code_tree", "debug",
    "dev_analyze", "dev_debug", "dev_lint", "dev_links",
    "dev_refactor", "dev_summarize",
    "doc_read", "doc_search",
    "health", "search", "crawl",
    "memory_search", "memory_store", "memory_clear",
    "models", "specialist", "prompts",
})


def canonical_tool_name(name: str) -> str:
    """Return the non-namespaced leaf used for policy classification."""
    normalized = str(name or "").strip().lower()
    return re.split(r"[./:]", normalized)[-1]


def forbidden_reason(name: str) -> str | None:
    """Explain why *name* is outside the coding-client capability scope."""
    canonical = canonical_tool_name(name)
    if canonical in FORBIDDEN_EXACT:
        return f"tool '{name}' is disabled by the ai-coder coding-only policy"
    if any(canonical.startswith(prefix) for prefix in FORBIDDEN_PREFIXES):
        return f"tool '{name}' belongs to a forbidden admin/ops scope"
    return None


def require_allowed_tool(
    name: str,
    allowed_names: Iterable[str] | None,
    *,
    allow_internal: bool = False,
) -> tuple[bool, str]:
    """Validate a tool against the global policy and a per-run capability set."""
    reason = forbidden_reason(name)
    if reason:
        return False, reason

    canonical = canonical_tool_name(name)
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


def filter_tool_catalog(tools: Iterable[dict], allowed_names: Iterable[str]) -> list[dict]:
    """Return well-formed, policy-compliant schemas from an MCP catalogue."""
    allowed = set(allowed_names)
    allowed_canonical = {canonical_tool_name(item) for item in allowed}
    result: list[dict] = []
    for tool in tools:
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
        if not isinstance(name, str) or canonical_tool_name(name) not in allowed_canonical:
            continue
        ok, _ = require_allowed_tool(name, allowed)
        if ok:
            result.append(normalized)
    return result
