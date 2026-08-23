"""Deterministic capability resolution for progressive tool disclosure."""
from __future__ import annotations
from dataclasses import dataclass
import re
from typing import Iterable

DEFAULT_TOOL_BUDGET = 12
MAX_ACTIVE_TOOLS = 20
MAX_EXPANSION_ROUNDS = 3

# Progressive disclosure deliberately avoids an unconditional primitive bundle.
# A URL prompt should not receive shell/file mutation schemas merely because they
# exist. Stable host-side meta tools remain available so the model can discover
# and request additional capabilities without seeing the full catalogue.
BASE_TOOL_NAMES: tuple[str, ...] = ()
META_TOOL_NAMES = ("toolbox_search", "capability_request", "toolbox_improvise")
META_TOOL_SCHEMAS = (
    {
        "name": "toolbox_search",
        "description": "Search inactive AICoder tools/capabilities without exposing the full tool schemas.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 12}},
            "required": ["query"],
        },
        "capabilities": ["research"],
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "capability_request",
        "description": "Request additional named tools or capability groups for this task. The host reapplies enable/deny policy and active-tool limits.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "capabilities": {"type": "array", "items": {"type": "string"}},
                "tools": {"type": "array", "items": {"type": "string"}},
                "reason": {"type": "string"},
            },
        },
        "capabilities": ["research"],
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "toolbox_improvise",
        "description": "Explain the safe next step when no existing tool matches a capability gap; never installs or enables executable code.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        "capabilities": ["research"],
        "annotations": {"readOnlyHint": True},
    },
)

CAPABILITIES = frozenset({
    "web", "local_code_read", "local_code_write", "remote_code", "debug",
    "system_diagnostics", "packages", "services", "containers", "network",
    "storage", "memory", "models", "research", "settings", "git", "testing",
    "skills", "subagents",
})

# One centralized compatibility map while legacy schemas are migrated to explicit
# capability metadata. Unknown tools are not guessed into privileged groups.
TOOL_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "file_read": ("local_code_read",), "file_tree": ("local_code_read",),
    "code_grep": ("local_code_read",), "file_edit": ("local_code_write",),
    "directory_create": ("local_code_write",), "shell": ("local_code_write", "debug"),
    "binary_exec": ("local_code_write", "testing"), "task_runner": ("local_code_write", "testing"),
    "git": ("git", "local_code_read"), "lint": ("testing", "debug"), "test": ("testing", "debug"),
    "web_fetch_local": ("web", "research"), "search": ("web", "research"), "crawl": ("web", "research"),
    "code_read": ("remote_code",), "code_search": ("remote_code",), "code_tree": ("remote_code",),
    "memory_search": ("memory", "research"), "memory_store": ("memory",),
    "models": ("models",), "specialist": ("models",), "health": ("system_diagnostics",),
    "skill_read": ("skills",), "subagent_run": ("subagents",),
}

_URL_RE = re.compile(r"https?://\S+", re.I)
_GIT_RE = re.compile(r"(?:github\.com|gitlab\.com|codeberg\.org|\bgit\s+(?:status|diff|log|branch|commit|repo))", re.I)
_PATH_RE = re.compile(r"(?:^|\s)(?:\.{0,2}/|~/|/[\w.-]+/|[\w.-]+\.(?:py|js|ts|tsx|rs|go|java|c|cpp|toml|ya?ml|json|md|sh))", re.I)
_DEBUG_RE = re.compile(r"traceback|stack\s*trace|exception|segfault|failing\s+test|test\s+fail|fehler|error", re.I)
_TEST_RE = re.compile(r"\b(?:test|tests|pytest|unittest|lint|ruff|mypy|verify|verifizier)\w*\b", re.I)
_SETTINGS_RE = re.compile(r"\b(?:setting|settings|configuration|config|einstellung|konfiguration)\w*\b", re.I)
_CONTAINER_RE = re.compile(r"\b(?:docker|container|compose|podman)\w*\b", re.I)
_SYSTEM_RE = re.compile(r"\b(?:cpu|memory|ram|disk|performance|system|prozess|process|load|kernel)\w*\b", re.I)
_RESEARCH_RE = re.compile(r"\b(?:latest|current|search|find|research|recherch|aktuell|version|release|changelog|documentation|doku)\w*\b", re.I)
_WRITE_RE = re.compile(r"\b(?:write|edit|create|implement|build|fix|patch|refactor|rename|delete|schreib|änder|erstell|implementier|bau|reparier|lösch)\w*\b", re.I)


@dataclass(frozen=True)
class CapabilityResolution:
    capabilities: tuple[str, ...]
    signals: tuple[str, ...]
    confidence: float


def resolve_capabilities(prompt: str, *, resume: bool = False) -> CapabilityResolution:
    text = (prompt or "").strip(); caps: list[str] = []; signals: list[str] = []
    def add(cap: str, signal: str) -> None:
        if cap not in caps: caps.append(cap)
        if signal not in signals: signals.append(signal)
    if _URL_RE.search(text): add("web", "url")
    if _GIT_RE.search(text): add("git", "git")
    if _PATH_RE.search(text): add("local_code_read", "path")
    if _DEBUG_RE.search(text): add("debug", "failure"); add("local_code_read", "failure")
    if _TEST_RE.search(text): add("testing", "testing")
    if _SETTINGS_RE.search(text): add("settings", "settings")
    if _CONTAINER_RE.search(text): add("containers", "containers"); add("system_diagnostics", "containers")
    if _SYSTEM_RE.search(text): add("system_diagnostics", "system")
    if _RESEARCH_RE.search(text): add("research", "research"); add("web", "research")
    if _WRITE_RE.search(text): add("local_code_write", "write"); add("local_code_read", "write")
    if resume and not caps: add("local_code_read", "resume")
    confidence = min(1.0, 0.35 + 0.15 * len(signals)) if signals else 0.0
    return CapabilityResolution(tuple(caps), tuple(signals), confidence)


def tool_capabilities(tool: dict) -> tuple[str, ...]:
    explicit = tool.get("capabilities")
    if isinstance(explicit, (list, tuple)):
        valid = tuple(str(x) for x in explicit if str(x) in CAPABILITIES)
        if valid: return valid
    return TOOL_CAPABILITIES.get(str(tool.get("name") or ""), ())


def select_tools(tools: Iterable[dict], resolution: CapabilityResolution, *, budget: int = DEFAULT_TOOL_BUDGET) -> list[dict]:
    budget = max(0, min(MAX_ACTIVE_TOOLS, int(budget)))
    wanted = set(resolution.capabilities)
    ranked = []
    for index, tool in enumerate(tools):
        caps = set(tool_capabilities(tool)); overlap = len(caps & wanted)
        if overlap:
            ranked.append((-overlap, index, tool))
    ranked.sort(key=lambda row: (row[0], row[1]))
    return [row[2] for row in ranked[:budget]]


def build_working_set(tools: Iterable[dict], resolution: CapabilityResolution, *, budget: int = DEFAULT_TOOL_BUDGET) -> list[dict]:
    """Build the initial task-specific set plus stable host meta tools.

    ``budget`` is the total model-facing schema budget, including meta tools.
    Unknown tools are never selected implicitly.
    """
    catalogue = [tool for tool in tools if isinstance(tool, dict) and tool.get("name")]
    by_name = {str(tool["name"]): tool for tool in catalogue}
    limit = max(0, min(MAX_ACTIVE_TOOLS, int(budget)))
    chosen: list[dict] = []
    seen: set[str] = set()
    for schema in META_TOOL_SCHEMAS:
        if len(chosen) >= limit:
            break
        name = str(schema["name"])
        chosen.append(dict(schema)); seen.add(name)
    for name in BASE_TOOL_NAMES:
        if len(chosen) >= limit:
            break
        tool = by_name.get(name)
        if tool is not None and name not in seen:
            chosen.append(tool); seen.add(name)
    remaining = max(0, limit - len(chosen))
    for tool in select_tools(catalogue, resolution, budget=remaining):
        name = str(tool.get("name") or "")
        if name and name not in seen:
            chosen.append(tool); seen.add(name)
    return chosen[:limit]


def search_toolbox(tools: Iterable[dict], query: str, *, active_names: Iterable[str] = (), limit: int = 8) -> list[dict]:
    """Search the host-side catalogue without exposing every schema to the model."""
    terms = {term for term in re.findall(r"[a-z0-9_.-]+", (query or "").lower()) if len(term) > 1}
    active = {str(name) for name in active_names}
    ranked: list[tuple[int, str, dict]] = []
    for tool in tools:
        if not isinstance(tool, dict): continue
        name = str(tool.get("name") or "")
        if not name or name in active: continue
        desc = str(tool.get("description") or "")
        caps = tool_capabilities(tool)
        haystack = " ".join((name, desc, *caps)).lower()
        score = sum(3 if term in name.lower() else 1 for term in terms if term in haystack)
        if score:
            ranked.append((-score, name, tool))
    ranked.sort(key=lambda row: (row[0], row[1]))
    return [{"name": row[1], "description": str(row[2].get("description") or ""), "capabilities": list(tool_capabilities(row[2]))} for row in ranked[:max(1, min(12, int(limit)))] ]


def expansion_tools(tools: Iterable[dict], requested: Iterable[str], *, active_names: Iterable[str], slots: int) -> list[dict]:
    """Resolve explicit tool/capability requests into schemas, respecting the active-set cap."""
    requested_set = {str(item).strip() for item in requested if str(item).strip()}
    active = {str(item) for item in active_names}
    result: list[dict] = []
    for tool in tools:
        if not isinstance(tool, dict): continue
        name = str(tool.get("name") or "")
        if not name or name in active: continue
        caps = set(tool_capabilities(tool))
        if name in requested_set or caps.intersection(requested_set):
            result.append(tool)
            if len(result) >= max(0, slots): break
    return result


def improvisation_advice(query: str, matches: Iterable[dict]) -> dict:
    """Fail-safe self-extension guidance; never silently creates or enables executable code."""
    found = [str(item.get("name") or "") for item in matches if isinstance(item, dict)]
    if found:
        return {"action": "reuse", "tools": found, "reason": "Existing tools match the requested capability."}
    return {
        "action": "improvise",
        "tools": [],
        "reason": "No matching tool found. Combine primitive tools first; if the gap repeats, draft a skill or a disabled plugin/MCP provider with tests. Activation still requires normal host policy/approval.",
        "query": str(query or ""),
    }
