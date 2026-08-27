"""Opt-in native-light agent runtime shared by CLI and GUI.

This module owns the agent state machine. Presentation stays in callers through
runtime events, so future skills/subagents can extend one loop instead of two.
"""
from __future__ import annotations

import json
import queue
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .agent_journal import ContinuationJournalStore
from .model_capabilities import model_context_window, supports_tools
from .performance import RuntimePerformance, model_usage_metrics
from .agent_plan import AgentPlan, PlanStore, plan_prompt_context, resume_prompt_context
from .client import ClientError, TriForceClient
from .capabilities import (
    DEFAULT_TOOL_BUDGET, MAX_ACTIVE_TOOLS, MAX_EXPANSION_ROUNDS,
    META_TOOL_NAMES, build_working_set, expansion_tools, improvisation_advice,
    resolve_capabilities, search_toolbox,
)
from .evidence_memory import ProjectEvidenceStore
from .failure_tracking import FailureTracker
from .hooks import HookBus
from .executor import (
    AGENT_CHECKPOINT_INTERVAL,
    MAX_CONTEXT_MESSAGES,
    MAX_ITERATIONS,
    STALL_FALLBACK_REPEATS,
    STALL_NUDGE_REPEATS,
    STALL_RECOVERY_PROMPT,
    RESEARCH_RECOVERY_PROMPT,
    REPEATED_ERROR_RECOVERY_PROMPT,
    AgentLoopGuard,
    adaptive_request_timeout,
    agent_checkpoint,
    build_system_prompt,
    chat_with_timeout,
    format_untrusted_tool_results,
    is_action_request,
    is_destructive,
    is_short_confirmation,
    load_tools,
    merge_tool_calls,
    normalize_tool_calls,
    parse_tool_calls,
    run_tool,
    strip_tool_calls,
    trim_messages,
)
from .model_transport import ModelTransport, native_model_transport_from_env
from .privileges import assess_execution
from .tool_policy import require_allowed_tool

RuntimeEventFn = Callable[[str, dict[str, Any]], None]

MAX_AUTO_RESUMES = 3
MAX_SAFETY_CONTINUATIONS = 6
READ_ONLY_PROGRESS_NUDGE_BATCHES = 6
_AUTO_RESUMABLE_PAUSE_MARKERS = (
    "transient model/backend failure",
    "no usable final response",
    "did not perform a successful post-change verification",
    "done was requested without a successful post-change verification",
    "merge self-reported incomplete",
    "same tool operation",
    "kept repeating without progress",
    "safety pause after an unusually long run",
)

def auto_resumable_pause(reason: str) -> bool:
    text = str(reason or "").strip().lower()
    if not text:
        return False
    hard_stops = (
        "stopped by user", "user rejected", "aborted by user",
        "approval denied", "workspace escape", "security policy",
    )
    if any(marker in text for marker in hard_stops):
        return False
    return any(marker in text for marker in _AUTO_RESUMABLE_PAUSE_MARKERS)


def auto_resume_limit(reason: str) -> int:
    text = str(reason or "").lower()
    if "safety pause after an unusually long run" in text:
        return MAX_SAFETY_CONTINUATIONS
    if ("transient incomplete chat response" in text or "no recognized assistant response envelope" in text or "keys=['_transport_telemetry']" in text):
        return 5
    return MAX_AUTO_RESUMES


def auto_resume_prompt(
    reason: str, attempt: int, limit: int | None = None, *, unlimited: bool = False,
) -> str:
    active_limit = int(limit or auto_resume_limit(reason))
    limit_label = "unlimited" if unlimited else str(active_limit)
    text = str(reason or "")
    if "safety pause after an unusually long run" in text.lower():
        return (
            f"Automatic continuation slice {attempt}/{limit_label}. This is NOT a fresh analysis pass. "
            "Continue from the preserved conversation and tool evidence. Do not restart architecture discovery, "
            "git-status loops, baseline tests, or reread unchanged files already present in context. "
            "Move the assigned task toward a terminal state now: implement the smallest evidence-backed change, "
            "then verify it; or, if no code change is justified, finish with DONE and cite the concrete evidence. "
            "Prefer mutation/verification over further broad inspection unless a specific blocker requires one read. "
            f"Previous pause reason: {text[:1200]}"
        )
    return (
        f"Automatic runtime resume {attempt}/{limit_label}. "
        "Continue the existing task from the preserved context. Do not repeat the same failed "
        "action unchanged. Inspect existing tool results, choose a different approach when needed, "
        "finish required verification, and continue until the assigned task is actually complete. "
        f"Previous pause reason: {text[:1200]}"
    )


def continuation_messages(result: "AgentRunResult") -> list[dict[str, Any]]:
    messages = [dict(item) for item in (result.messages or []) if isinstance(item, dict)]
    if messages and messages[0].get("role") == "system":
        messages = messages[1:]
    return messages[-MAX_CONTEXT_MESSAGES:]

ApprovalFn = Callable[[str, dict], bool]
StopFn = Callable[[], bool]

_BEHAVIOR_VERIFY_TOOLS = {"test", "lint", "dev_lint", "dev_analyze"}
_SHELL_VERIFY_RE = re.compile(
    r"(?:^|\s)(?:pytest|unittest|ruff|mypy|pylint|flake8|pyright|shellcheck|"
    r"cargo\s+(?:test|check|clippy)|go\s+test|npm\s+test|pnpm\s+test|yarn\s+test|"
    r"make\s+(?:test|check)|python(?:3)?\s+-m\s+(?:pytest|unittest|compileall|py_compile)|"
    r"python(?:3)?\s+(?:[^\s]*/)?test_[^\s]+\.py)\b",
    re.IGNORECASE,
)


def _is_behavior_verification_call(name: str, args: dict) -> bool:
    if name in _BEHAVIOR_VERIFY_TOOLS:
        return True
    if name in {"shell", "task_runner"}:
        return bool(_SHELL_VERIFY_RE.search(str(args.get("command") or "")))
    if name == "binary_exec":
        program = str(args.get("program") or "").lower()
        argv_items = [str(item) for item in (args.get("arguments") or [])]
        argv = " ".join(argv_items)
        if _SHELL_VERIFY_RE.search(f"{program} {argv}"):
            return True
        # Task-specific read-only verifiers commonly use ``python3 -B -c`` with
        # assertions instead of a test framework. Treat only assertion/readback
        # shaped invocations as verification evidence; arbitrary Python execution
        # remains outside this path and still follows normal approval policy.
        if program in {"python", "python3"} and "-c" in argv_items:
            script = argv_items[argv_items.index("-c") + 1] if argv_items.index("-c") + 1 < len(argv_items) else ""
            verification_markers = ("assert ", ".read_bytes(", ".read_text(", ".is_file(", ".is_dir(", ".is_symlink(")
            return any(marker in script for marker in verification_markers)
        return False
    return False


_COMMAND_EXECUTION_TOOLS = {"shell", "task_runner", "binary_exec", "custom_exec", "local_exec"}


def _has_mutation_effect(name: str, args: dict) -> bool:
    """Classify progress effects separately from conservative approval risk.

    Command runners always require write approval because arbitrary programs may
    mutate state. That conservative policy is not evidence that a successful
    read-only command actually changed state, however. For progress tracking,
    inspect the concrete command and explicit provider effect metadata instead.
    """
    canonical_name = re.split(r"[./:]", str(name or "").lower())[-1]
    if canonical_name not in _COMMAND_EXECUTION_TOOLS:
        risk = assess_execution(name, args, destructive=is_destructive(str(args.get("command", ""))))
        return bool(risk.mutation or risk.destructive)

    command = str(args.get("command") or "").strip()
    if canonical_name == "binary_exec":
        program = str(args.get("program") or "").strip()
        arguments = " ".join(str(item) for item in (args.get("arguments") or []))
        command = f"{program} {arguments}".strip()
    effect_args = {
        "command": command,
        "_mutating": args.get("_mutating"),
        "_destructive": args.get("_destructive"),
        "_security_change": args.get("_security_change"),
    }
    risk = assess_execution(
        "command_effect",
        effect_args,
        destructive=is_destructive(command),
    )
    return bool(risk.mutation or risk.destructive)


_INSPECTION_TOOLS = {
    "git", "file_read", "code_grep", "code_read", "code_search",
    "file_tree", "code_tree",
}

_VERIFICATION_REQUIRED_PROMPT = (
    "Verification required: a state-changing tool succeeded, but no sufficient post-change "
    "verification has been observed. For data/config artifacts, confirm the intended artifact "
    "state. For source-code or behavior changes, run an applicable lint/test/compile/reproducer "
    "or other executable check; rereading the source alone is not behavior verification. "
    "Do not report DONE until the relevant verification succeeds."
)

_POLLING_INTENT_RE = re.compile(
    r"\b(?:poll(?:ing)?|monitor(?:ing)?|watch|check\s+again|recheck|wait\s+until|"
    r"wiederholt(?:e|en)?|erneut\s+pr[uü]f|[uü]berwach|beobacht)\b",
    re.IGNORECASE,
)

_STRUCTURED_REQUIREMENT_RE = re.compile(r"(?m)^\s*(?:\\?[-*+]\s+|\d+\\?[.)]\s+)")

def _needs_completion_audit(prompt: str) -> bool:
    text = str(prompt or "")
    return len(_STRUCTURED_REQUIREMENT_RE.findall(text)) >= 3

def _has_embedded_text_tool_protocol(text: str) -> bool:
    """Detect a likely v2 tool attempt surrounded by prose without executing it."""
    raw = str(text or "")
    return bool(
        re.search(r"(?m)^\s*TOOL_CALL\s+[A-Za-z0-9_.:-]+\s*$", raw)
        and re.search(r"(?m)^\s*END_TOOL_CALL\s*$", raw)
    )

COMPLETION_AUDIT_MAX_CHARS = 50_000


def _completion_audit_prompt(prompt: str) -> str:
    # Preserve long, requirement-heavy tasks during the final completion audit.
    # The model context budget still provides the outer safety bound.
    task = str(prompt or "")[:COMPLETION_AUDIT_MAX_CHARS]
    return (
        "Completion audit: compare every explicit requirement in the original task against the tool evidence already present. "
        "If anything remains unfinished, continue with the required tool. If all requirements are complete, return the final answer beginning with DONE:. "
        "Do not repeat already verified work.\n\nOriginal task:\n" + task
    )

_FINAL_RESPONSE_REPAIR_PROMPT = (
    "Your previous response was empty or contained an invalid/incomplete tool call. "
    "Discard that malformed output completely; do not continue or complete its fragment. "
    "If another tool is required, generate one NEW tool call from the beginning using exactly:\n"
    "TOOL_CALL tool_name\n{\"argument\": \"value\"}\nEND_TOOL_CALL\n"
    "Use the exact tool name and only its argument JSON object. No prose before or after it. "
    "Otherwise finish the user's task with a normal textual answer. Do not return an empty response."
)


def _recover_unclosed_tool_calls(text: str) -> list[dict]:
    """Recover only complete JSON after an unclosed final <tool_call> tag.

    This is deliberately conservative: truncated or mixed prose/JSON is never guessed.
    """
    raw = str(text or "")
    lowered = raw.lower()
    start = lowered.rfind("<tool_call>")
    if start < 0 or "</tool_call>" in lowered[start:]:
        return []
    payload = raw[start + len("<tool_call>"):].strip()
    if not payload:
        return []
    try:
        decoded = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return []
    return normalize_tool_calls(decoded)


def _has_incomplete_tool_markup(text: str) -> bool:
    """Return True only when the response actually starts a tool protocol and truncates it.

    Mentions such as ``tool_call`` or ``parse_tool_calls`` inside a normal review are
    ordinary prose and must never trigger final-response repair.
    """
    raw = str(text or "")
    stripped = raw.lstrip()
    lowered = stripped.lower()

    # Legacy protocol: only treat it as protocol markup when the response itself
    # starts with a legacy tool-call envelope.
    if lowered.startswith("<tool_call>"):
        return "</tool_call>" not in lowered

    # Protocol v2: only a leading TOOL_CALL marker enters protocol mode. A normal
    # final answer may legitimately discuss TOOL_CALL/END_TOOL_CALL as code terms.
    if lowered.startswith("tool_call ") or lowered == "tool_call":
        return not bool(parse_tool_calls(raw))

    # A provider may return only the tail of a legacy envelope (observed as
    # ``}}\n</tool_call>``). Treat punctuation-only content before an orphan closing
    # tag as protocol debris, while normal prose that merely mentions the tag
    # remains a valid final answer.
    if lowered.endswith("</tool_call>"):
        prefix = stripped[:-len("</tool_call>")].strip()
        if not any(ch.isalnum() for ch in prefix):
            return True

    return False


@dataclass
class AgentRunResult:
    status: str
    response: str
    model: str
    messages: list[dict]
    tools: list[dict]
    system_prompt: str
    iterations: int = 0
    latency_ms: int = 0
    fallback_used: bool = False
    plan_id: str = ""
    error: str = ""


@dataclass
class NativeLightRuntime:
    client: TriForceClient
    initial_prompt: str
    model: str | None
    fallback_model: str | None  # deprecated compatibility input; never activated
    workspace_root: str
    plan_workspace_root: str | None = None
    protected_workspace_root: str | None = None
    completion_guard: Callable[[], None] | None = None
    model_client: ModelTransport | None = None
    tools: list[dict] | None = None
    system_prompt: str | None = None
    conversation: list[dict] | None = None
    load_tools_on_start: bool = True
    enabled_tool_names: list[str] | None = None
    quick_chat: bool = False
    approval_fn: ApprovalFn | None = None
    event_fn: RuntimeEventFn | None = None
    stop_requested: StopFn | None = None
    plan_store: PlanStore = field(default_factory=PlanStore)
    journal_store: ContinuationJournalStore | None = None
    persistent_plan: bool = True
    resume: bool = False
    resume_plan_id: str | None = None
    base_timeout: int = 300
    max_output_tokens: int = 16384
    tools_unavailable_reason: str = ""
    max_iterations: int = MAX_ITERATIONS
    progressive_tool_disclosure: bool = True
    native_openrouter_tool_calling: bool = False
    tool_budget: int = DEFAULT_TOOL_BUDGET
    max_expansion_rounds: int = MAX_EXPANSION_ROUNDS
    hooks: HookBus = field(default_factory=HookBus)
    _tool_capability_warned: bool = False
    _tool_catalog: list[dict] = field(default_factory=list, init=False, repr=False)
    _expansion_rounds: int = field(default=0, init=False, repr=False)

    def _emit(self, event_kind: str, **payload: Any) -> None:
        """Emit a runtime event without colliding with payload fields named ``kind``."""
        if self.event_fn is not None:
            self.event_fn(event_kind, payload)

    def _stopped(self) -> bool:
        return bool(self.stop_requested and self.stop_requested())

    def _chat_interruptibly(self, model_client, timeout: int, **kwargs: Any) -> dict[str, Any]:
        """Run one blocking model request while allowing cooperative cancellation."""
        if self.stop_requested is None:
            return chat_with_timeout(model_client, timeout, **kwargs)

        result_queue: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

        def invoke() -> None:
            try:
                result_queue.put((True, chat_with_timeout(model_client, timeout, **kwargs)))
            except Exception as exc:
                result_queue.put((False, exc))

        thread = threading.Thread(target=invoke, name="aicoder-model-call", daemon=True)
        thread.start()
        while thread.is_alive():
            if self._stopped():
                cancel = getattr(model_client, "cancel_current_request", None)
                if callable(cancel):
                    try:
                        request_id = kwargs.get("request_id")
                        try:
                            cancel(request_id)
                        except TypeError:
                            cancel()
                    except Exception:
                        # Cancellation is best-effort; Stop must still return promptly.
                        pass
                raise InterruptedError("Agent stopped by user")
            thread.join(0.1)
        ok, value = result_queue.get()
        if ok:
            return value
        raise value

    def _native_tool_calling_enabled(self, model: str | None) -> bool:
        return bool(
            self.native_openrouter_tool_calling
            and str(model or "").startswith("openrouter/")
        )

    @staticmethod
    def _system_for_tool_protocol(system: str, *, native: bool) -> str:
        if not native:
            return system
        text_block = (
            "## Tool Call Format:\n"
            "When you need a tool, output one or more complete blocks and nothing else:\n"
            "TOOL_CALL tool_name\n"
            '{"argument": "value"}\n'
            "END_TOOL_CALL\n\n"
            "Rules:\n"
            "- Use the exact tool name from the tool list.\n"
            "- The JSON object contains only that tool's arguments; do not wrap it in name/arguments.\n"
            "- Use {} when the tool takes no arguments.\n"
            "- Multiple blocks in one response are allowed only for independent calls whose arguments do not depend on another call's result.\n"
            "- If a later action depends on an earlier result, emit only the first call and wait for its result.\n"
            "- Do not add prose before, between, or after tool-call blocks.\n"
            "- Never continue a broken prior tool call; always start a new call from TOOL_CALL.\n"
            "- Never invent fields or omit required fields."
        )
        native_block = (
            "## Tool Calling\n"
            "Use only the provider-native function/tool calls supplied with this request.\n"
            "Do not emit <tool_call> markup or manually constructed JSON tool calls in assistant text."
        )
        return system.replace(text_block, native_block)

    def _tools_for_request(self, tools: list[dict] | None, model: str | None) -> list[dict] | None:
        """Return native provider tool schemas only for explicit OpenRouter opt-in.

        The default agent protocol is provider-independent text tool calling for
        every model. The legacy/native path is preserved behind the experimental
        ``native_openrouter_tool_calling`` setting so it can be tested without
        competing with the text protocol during normal runs.
        """
        if not tools or not self.load_tools_on_start:
            return None
        if not self._native_tool_calling_enabled(model):
            return None
        if supports_tools(self.client, model, allow_openrouter=True):
            # Freeze the active schema set for this model turn. Dynamic expansion
            # mutates the runtime working set only for subsequent turns.
            return list(tools)
        if not self._tool_capability_warned:
            self._tool_capability_warned = True
            self._emit(
                "model_without_tool_support",
                model=model or "?",
                tool_count=len(tools),
            )
        return None

    def _plan_workspace(self) -> str:
        return str(Path(self.plan_workspace_root or self.workspace_root or ".").expanduser().resolve(strict=False))

    def _prepare_tools(self) -> list[dict]:
        if self.tools is None and self.load_tools_on_start:
            started = time.monotonic()
            catalogue = load_tools(self.client)
            if self.enabled_tool_names is not None:
                enabled = set(self.enabled_tool_names)
                catalogue = [tool for tool in catalogue if tool.get("name") in enabled]
            self._tool_catalog = list(catalogue)
            if self.progressive_tool_disclosure:
                capability_prompt = self.initial_prompt
                if self.resume and self.persistent_plan:
                    try:
                        if self.resume_plan_id == "current":
                            resume_plan = self.plan_store.load_current(self._plan_workspace())
                        elif self.resume_plan_id:
                            resume_plan = self.plan_store.load(self._plan_workspace(), self.resume_plan_id)
                        else:
                            resume_plan = self.plan_store.load_current(self._plan_workspace())
                    except (OSError, ValueError):
                        resume_plan = None
                    if resume_plan is not None and resume_plan.task:
                        capability_prompt = (
                            f"{resume_plan.task}\n\nContinuation instruction: {self.initial_prompt}"
                        )
                resolution = resolve_capabilities(capability_prompt, resume=self.resume)
                tools = build_working_set(catalogue, resolution, budget=self.tool_budget)
                self._emit(
                    "capabilities_ready", capabilities=list(resolution.capabilities),
                    signals=list(resolution.signals), confidence=resolution.confidence,
                    active_tools=[str(tool.get("name") or "") for tool in tools],
                )
            else:
                tools = list(catalogue)
            self.tools = tools
            self._emit(
                "tools_ready", count=len(tools), catalogue_count=len(catalogue),
                elapsed=time.monotonic() - started,
            )
        elif self.tools is None:
            self.tools = []
        else:
            self._tool_catalog = list(self.tools)
        return self.tools

    def _run_meta_tool(self, name: str, args: dict, tools: list[dict]) -> tuple[str, bool, bool]:
        """Execute stable capability-discovery tools inside the host runtime."""
        active_names = {str(tool.get("name") or "") for tool in tools}
        if name == "toolbox_search":
            matches = search_toolbox(
                self._tool_catalog, str(args.get("query") or ""),
                active_names=active_names, limit=int(args.get("limit") or 8),
            )
            return json.dumps({"matches": matches}, ensure_ascii=False), False, False
        if name == "toolbox_improvise":
            query = str(args.get("query") or "")
            matches = search_toolbox(self._tool_catalog, query, active_names=active_names)
            return json.dumps(improvisation_advice(query, matches), ensure_ascii=False), False, False
        if name == "capability_request":
            if self._expansion_rounds >= max(0, int(self.max_expansion_rounds)):
                return "capability_request: expansion limit reached", True, False
            requested: list[str] = []
            for key in ("capabilities", "tools"):
                value = args.get(key)
                if isinstance(value, list):
                    requested.extend(str(item).strip() for item in value if str(item).strip())
            if not requested:
                return "capability_request: provide at least one capability or tool name", True, False
            slots = max(0, MAX_ACTIVE_TOOLS - len(active_names))
            additions = expansion_tools(
                self._tool_catalog, requested, active_names=active_names, slots=slots,
            )
            if not additions:
                return "capability_request: no enabled inactive tools matched the request", True, False
            tools.extend(additions)
            self._expansion_rounds += 1
            added_names = [str(tool.get("name") or "") for tool in additions]
            self._emit(
                "tools_expanded", added=added_names, active_count=len(tools),
                round=self._expansion_rounds, reason=str(args.get("reason") or ""),
            )
            return json.dumps({"added": added_names, "active_count": len(tools)}, ensure_ascii=False), False, True
        return f"{name}: unknown runtime meta tool", True, False

    def _prepare_plan(self) -> tuple[AgentPlan | None, bool]:
        if not self.persistent_plan:
            return None, False
        if self.quick_chat and not self.resume:
            return None, False
        plan: AgentPlan | None = None
        if self.resume:
            if self.resume_plan_id == "current":
                plan = self.plan_store.load_current(self._plan_workspace())
                if plan is None:
                    raise ValueError("no current persistent plan to resume in this workspace")
            elif self.resume_plan_id:
                plan = self.plan_store.load(self._plan_workspace(), self.resume_plan_id)
                if plan is None:
                    raise ValueError(f"resume plan not found in this workspace: {self.resume_plan_id}")
            else:
                plan = self.plan_store.load_current(self._plan_workspace())
            if plan is not None and plan.status in {"running", "paused", "failed"}:
                previous_reason = plan.pause_reason
                plan.status = "running"
                plan.pause_reason = previous_reason
                plan.model = str(self.model or plan.model or "")
                plan.resume_count += 1
                plan.record_event("resume", "Plan resumed")
                self.plan_store.save(plan)
                self._emit("plan", action="resumed", plan=plan)
                return plan, True
            if self.resume_plan_id and plan is not None:
                raise ValueError(
                    f"resume plan is not resumable (status={plan.status}): {plan.id}"
                )
        plan = self.plan_store.create(
            self.initial_prompt, self._plan_workspace(), str(self.model or "")
        )
        self._emit("plan", action="created", plan=plan)
        return plan, False

    @staticmethod
    def _with_plan_context(base_system: str, plan: AgentPlan | None) -> str:
        if plan is None:
            return base_system
        return base_system.rstrip() + "\n\n" + plan_prompt_context(plan)

    def _save_plan(self, plan: AgentPlan | None) -> None:
        if plan is not None:
            self.plan_store.save(plan)

    def _journal(self) -> ContinuationJournalStore:
        if self.journal_store is not None:
            return self.journal_store
        return ContinuationJournalStore(self.plan_store.root.parent / "journals")

    def _save_journal(
        self,
        plan: AgentPlan | None,
        messages: list[dict],
        *,
        pending_input: str = "",
        tool_batches: list[dict[str, Any]] | None = None,
    ) -> None:
        if plan is None or plan.status == "completed":
            return
        try:
            self._journal().save_checkpoint(
                plan_id=plan.id,
                workspace=plan.workspace,
                messages=messages,
                pending_input=pending_input,
                tool_batches=tool_batches or [],
            )
            plan.record_event("journal", "Continuation checkpoint saved")
            self._save_plan(plan)
        except (OSError, ValueError, TypeError):
            # Journal persistence must never corrupt or abort the execution plan.
            plan.record_event("journal", "Continuation checkpoint could not be saved", is_error=True)
            self._save_plan(plan)

    def _clear_journal(self, plan: AgentPlan | None) -> None:
        if plan is None:
            return
        try:
            self._journal().clear(plan.workspace, plan.id)
        except (OSError, ValueError):
            pass

    def _record_tool_progress(
        self,
        plan: AgentPlan | None,
        name: str,
        args: dict,
        result: str,
        is_error: bool,
        mutation_seen: bool,
    ) -> tuple[bool, bool]:
        """Track safety-relevant progress independently of plan persistence.

        Runtime safety must be identical in classic and persistent-plan modes.
        Classify the effect first; the AgentPlan is only a persistence/UI view of
        that state and must never be the source of truth for mutation detection.
        """
        mutation_effect = _has_mutation_effect(name, args)
        previous_mutation_seen = mutation_seen
        verification_seen = False

        if plan is not None:
            # Persist only execution metadata, never raw tool output. Tool results may
            # contain source snippets, tokens, credentials, or other sensitive data.
            plan.record_event(
                "tool",
                f"{name} {'failed' if is_error else 'completed'}",
                tool=name,
                is_error=is_error,
            )

        if is_error:
            if plan is not None:
                self._save_plan(plan)
            return mutation_seen, False

        deterministic_verified = False
        behavior_verified = _is_behavior_verification_call(name, args)
        if behavior_verified:
            # Verification/check execution is not itself the task implementation.
            # It verifies a prior mutation when one exists; otherwise it is simply
            # a check, even if the privilege layer conservatively treats the tool
            # as potentially mutating for approval purposes.
            verification_seen = previous_mutation_seen
        elif mutation_effect:
            mutation_seen = True
            if "verified" in str(result).lower():
                if name == "directory_create":
                    deterministic_verified = True
                elif name == "file_edit":
                    # Exact read-back proves artifact state. For source code this is
                    # not behavior verification, so code still requires lint/test or
                    # another executable check before DONE.
                    target = Path(str(args.get("path") or ""))
                    suffix = target.suffix.lower()
                    name_lower = target.name.lower()
                    deterministic_verified = suffix in {
                        ".txt", ".md", ".rst", ".csv", ".log",
                        ".json", ".jsonl", ".yaml", ".yml", ".toml",
                        ".ini", ".cfg", ".conf", ".properties",
                    } or name_lower == ".env" or name_lower.startswith(".env.")
            verification_seen = deterministic_verified

        if plan is None:
            return mutation_seen, verification_seen

        if behavior_verified and previous_mutation_seen:
            plan.set_step("inspect", "completed", f"Checked executable state via {name}")
            plan.set_step("verify", "completed", f"Verified via {name}")
        elif mutation_effect:
            plan.set_step("inspect", "completed", "Relevant state inspected before mutation")
            plan.set_step("implement", "completed", f"Successful mutation via {name}")
            if deterministic_verified:
                plan.set_step("verify", "completed", f"Deterministically verified by {name}")
            else:
                plan.set_step("verify", "in_progress", "Waiting for post-change verification")
        elif behavior_verified:
            plan.set_step("inspect", "completed", f"Checked executable state via {name}")
        elif name in _INSPECTION_TOOLS:
            plan.set_step("inspect", "completed", f"Checked state via {name}")
        else:
            inspect = next((step for step in plan.steps if step.id == "inspect"), None)
            if inspect is not None and inspect.status == "in_progress":
                plan.set_step("inspect", "completed", f"Successful inspection via {name}")
                plan.set_step("implement", "in_progress", "Inspection completed")
        self._save_plan(plan)
        return mutation_seen, verification_seen

    def _guard_completion(self, plan: AgentPlan | None, messages: list[dict], tools: list[dict], system: str, *,
                          model_used: str, iterations: int, total_latency: int, fallback_used: bool,
                          journal_batches: list[dict[str, Any]]) -> AgentRunResult | None:
        if self.completion_guard is None:
            return None
        try:
            self.completion_guard()
            return None
        except Exception as exc:
            reason = f"Workspace finalization failed: {type(exc).__name__}: {exc}"
            self._fail_plan(plan, reason)
            self._save_journal(plan, messages, pending_input=reason, tool_batches=journal_batches)
            self._emit("error", message=reason)
            return AgentRunResult(
                "failed", "", model_used, messages, tools, system,
                iterations=iterations, latency_ms=total_latency,
                fallback_used=fallback_used, plan_id=plan.id if plan else "", error=reason,
            )

    def _complete_plan(
        self,
        plan: AgentPlan | None,
        response: str,
        *,
        mutation_seen: bool,
        verification_seen: bool,
    ) -> None:
        if plan is None:
            return
        plan.status = "completed"
        plan.last_response = response[:4000]
        plan.pause_reason = ""
        if not mutation_seen:
            plan.set_step("inspect", "completed", "Task completed without a state mutation")
            plan.set_step("implement", "skipped", "No mutation required or observed")
            plan.set_step("verify", "skipped", "No post-mutation verification required")
        elif not verification_seen:
            verify = next((step for step in plan.steps if step.id == "verify"), None)
            if verify is not None and verify.status != "completed":
                plan.set_step("verify", "skipped", "No successful verification tool observed")
        plan.record_event("complete", "Agent run completed")
        self._save_plan(plan)
        self._clear_journal(plan)

    def _pause_plan(self, plan: AgentPlan | None, reason: str, response: str = "") -> None:
        if plan is None:
            return
        plan.status = "paused"
        plan.pause_reason = reason[:1000]
        if response:
            plan.last_response = response[:4000]
        plan.record_event("pause", reason)
        self._save_plan(plan)

    def _fail_plan(self, plan: AgentPlan | None, reason: str) -> None:
        if plan is None:
            return
        plan.status = "failed"
        plan.pause_reason = reason[:1000]
        plan.record_event("error", reason, is_error=True)
        self._save_plan(plan)

    def run(self) -> AgentRunResult:
        performance = RuntimePerformance()
        model_latency_warned = False
        filesystem_latency_warned = False

        def performance_snapshot() -> dict[str, Any]:
            return performance.snapshot()

        workspace = str(Path(self.workspace_root or ".").resolve())
        self.workspace_root = workspace
        tools = self._prepare_tools()
        session_hook = self.hooks.emit("SessionStart", {
            "workspace": workspace, "prompt": self.initial_prompt,
            "model": self.model or "", "tool_count": len(tools),
        })
        for diagnostic in session_hook.diagnostics:
            self._emit("hook_diagnostic", event="SessionStart", message=diagnostic)
        if session_hook.context:
            self.system_prompt = (self.system_prompt or build_system_prompt(tools, workspace)).rstrip() + (
                "\n\n## Session hook context\n" + "\n".join(session_hook.context)
            )
        if self.tools_unavailable_reason:
            reason = self.tools_unavailable_reason
            self._emit("runtime_status", category="error", status="failed", phase="bootstrap", runtime_mode=("native-light" if self.persistent_plan else "classic"), message=reason)
            self._emit("error", message=reason)
            return AgentRunResult(
                "failed", "", str(self.model or "?"), [], tools,
                self.system_prompt or build_system_prompt(tools, workspace),
                error=reason,
            )
        try:
            plan, resumed = self._prepare_plan()
        except ValueError as exc:
            reason = str(exc)
            self._emit("runtime_status", category="error", status="failed", phase="plan", runtime_mode=("native-light" if self.persistent_plan else "classic"), message=reason)
            self._emit("error", message=reason)
            return AgentRunResult(
                "failed", "", str(self.model or "?"), [], tools, "",
                error=reason,
            )
        base_system = self.system_prompt or build_system_prompt(tools, workspace)
        protocol_system = self._system_for_tool_protocol(
            base_system, native=self._native_tool_calling_enabled(self.model)
        )
        system = self._with_plan_context(protocol_system, plan)

        prior_context = [
            dict(message) for message in (self.conversation or [])
            if message.get("role") != "system"
        ]
        journal_batches: list[dict[str, Any]] = []
        if resumed and plan is not None:
            try:
                journal = self._journal().load(plan.workspace, plan.id)
            except (OSError, ValueError):
                journal = None
            if journal is not None:
                journal_batches = [dict(item) for item in journal.tool_batches if isinstance(item, dict)]
                if not prior_context:
                    prior_context = journal.resume_messages()
                plan.record_event("journal", "Continuation checkpoint restored")
                self._save_plan(plan)
                self._emit("journal", action="restored", messages=len(journal.messages), tool_batches=len(journal_batches))
        messages: list[dict] = [
            {"role": "system", "content": system},
            *prior_context[-MAX_CONTEXT_MESSAGES:],
        ]
        current_input = (
            resume_prompt_context(plan, self.initial_prompt)
            if resumed and plan is not None
            else self.initial_prompt
        )
        model_client, configured_model = native_model_transport_from_env(
            self.model_client or self.client, default_model=self.model
        )
        active_model = configured_model
        active_fallback = None  # automatic fallback routing intentionally removed
        model_used = active_model or "?"
        context_window_tokens = model_context_window(model_client, active_model)
        if context_window_tokens:
            reserved_tokens = max(4096, int(self.max_output_tokens or 0))
            usable_tokens = max(4096, int(context_window_tokens * 0.80) - reserved_tokens)
            context_char_budget = max(16384, min(400000, usable_tokens * 4))
        else:
            context_char_budget = 240000
        total_latency = 0
        fallback_used = False
        tool_was_called = False
        tool_nudge_sent = False
        mutation_seen, verification_seen = plan.progress_flags() if resumed and plan else (False, False)
        verification_nudge_sent = False
        fresh_inspection_after_resume = not resumed
        starting_plan_iteration = plan.iteration if plan is not None else 0
        loop_guard = AgentLoopGuard()
        failure_tracker = FailureTracker()
        read_only_batch_streak = 0
        evidence_store = None
        run_file_reads: set[tuple[str, int, int]] = set()
        try:
            evidence_store = ProjectEvidenceStore(self.workspace_root)
            self._emit("evidence_ready", **evidence_store.health())
        except Exception as exc:
            # Memory is an optimization, never a prerequisite for coding.
            self._emit("evidence_unavailable", error=f"{type(exc).__name__}: {exc}")

        pending_continuation = False
        if is_short_confirmation(self.initial_prompt):
            for message in reversed(prior_context):
                content = str(message.get("content", ""))
                if message.get("role") == "assistant" and content.lstrip().upper().startswith("DONE:"):
                    break
                if message.get("role") == "user" and not content.startswith("Tool "):
                    pending_continuation = is_action_request(content)
                    break
        intent_prompt = plan.task if resumed and plan is not None else self.initial_prompt
        must_use_tools = bool(tools) and (
            is_action_request(intent_prompt) or pending_continuation or resumed
        )
        allowed_tool_names = {
            str(tool.get("name")) for tool in tools if tool.get("name")
        }

        runtime_mode = "native-light" if self.persistent_plan else "classic"
        self._emit(
            "run_start",
            runtime_mode=runtime_mode,
            model=active_model or "backend-default",
            configured_model=self.model or "backend-default",
            effective_model=active_model or "backend-default",
            fallback=active_fallback or "",
            provider=(str(active_model).split("/", 1)[0] if active_model and "/" in str(active_model) else "default"),
            transport=type(model_client).__name__,
            native_tool_protocol=self._native_tool_calling_enabled(active_model),
            context_window_tokens=context_window_tokens or 0,
            context_char_budget=context_char_budget,
            tools=len(tools),
            workspace=workspace,
            source_workspace=self._plan_workspace(),
            plan_id=plan.id if plan else "",
            resumed=resumed,
        )
        self._emit(
            "runtime_status", category="run", status="ok", phase="bootstrap",
            runtime_mode=runtime_mode,
            message=(
                f"{runtime_mode} runtime initialized · tools={len(tools)} · "
                f"workspace={workspace} · protocol={'native' if self._native_tool_calling_enabled(active_model) else 'text'}"
            ),
        )

        iteration_limit = max(1, min(MAX_ITERATIONS, int(self.max_iterations or MAX_ITERATIONS)))
        final_response_repair_sent = False
        completion_audit_sent = False
        for i in range(iteration_limit):
            self._emit(
                "runtime_status", category="run", status="active", phase="iteration",
                runtime_mode=runtime_mode, iteration=i + 1,
                message=f"iteration {i + 1}/{iteration_limit} started",
            )
            if self._stopped():
                reason = "Agent stopped by user"
                self._pause_plan(plan, reason)
                self._save_journal(plan, messages, pending_input=current_input, tool_batches=journal_batches)
                self._emit("paused", reason=reason)
                return AgentRunResult(
                    "paused", reason, model_used, messages, tools, system,
                    iterations=i, latency_ms=total_latency,
                    fallback_used=fallback_used, plan_id=plan.id if plan else "",
                )

            if plan is not None:
                plan.iteration = starting_plan_iteration + i + 1
                plan.model = str(active_model or model_used or "")
                self._save_plan(plan)
                protocol_system = self._system_for_tool_protocol(
                    base_system, native=self._native_tool_calling_enabled(active_model)
                )
                messages[0]["content"] = self._with_plan_context(protocol_system, plan)
                system = messages[0]["content"]

            turn_input = current_input
            if turn_input:
                messages.append({"role": "user", "content": turn_input})
            messages = trim_messages(messages, max_chars=context_char_budget)
            continuation_turn = i > 0
            timeout = adaptive_request_timeout(
                self.base_timeout,
                prompt=turn_input,
                iteration=i,
                quick_chat=self.quick_chat,
                model=active_model,
                continuation=continuation_turn,
            )
            request_id = f"aicoder-{uuid.uuid4().hex[:16]}"
            self._emit(
                "model_start", iteration=i + 1, timeout=timeout, model=active_model or "",
                phase="continuation" if continuation_turn else "planning", request_id=request_id,
            )
            started = time.monotonic()
            try:
                request_tools = self._tools_for_request(tools, active_model)
                result = self._chat_interruptibly(
                    model_client,
                    timeout,
                    messages=messages,
                    model=active_model,
                    fallback_model=active_fallback,
                    temperature=0.3,
                    max_tokens=256 if self.quick_chat else self.max_output_tokens,
                    tools=request_tools,
                    tool_choice="auto",
                    request_id=request_id,
                )
            except InterruptedError:
                reason = "Agent stopped by user"
                self._pause_plan(plan, reason)
                self._save_journal(plan, messages, pending_input=current_input, tool_batches=journal_batches)
                self._emit("paused", reason=reason)
                return AgentRunResult(
                    "paused", reason, model_used, messages, tools, system,
                    iterations=i, latency_ms=total_latency,
                    fallback_used=fallback_used, plan_id=plan.id if plan else "",
                )
            except (ClientError, RuntimeError) as exc:
                reason = str(exc)
                category, _signature, retryable = FailureTracker.classify(reason)
                typed_retryable = bool(getattr(exc, "retryable", False))
                if typed_retryable and category != "transient":
                    category = "transient"
                    retryable = True
                if retryable and category == "transient":
                    retry_after = getattr(exc, "retry_after", None)
                    wait_hint = (
                        f" Recommended retry delay: {int(retry_after)}s."
                        if isinstance(retry_after, int) and retry_after > 0 else ""
                    )
                    pause_reason = (
                        "Transient model/backend failure after request retries were exhausted: "
                        f"{reason}{wait_hint}"
                    )
                    self._pause_plan(plan, pause_reason)
                    self._save_journal(
                        plan, messages, pending_input=current_input, tool_batches=journal_batches
                    )
                    self._emit(
                        "runtime_status", category="error", status="warning", phase="model",
                        runtime_mode=runtime_mode, iteration=i + 1, message=pause_reason,
                    )
                    self._emit(
                        "paused", reason=pause_reason, failure_category=category, resumable=True,
                        retry_after=getattr(exc, "retry_after", None),
                    )
                    return AgentRunResult(
                        "paused", pause_reason, model_used, messages, tools, system,
                        iterations=i + 1, latency_ms=total_latency,
                        fallback_used=fallback_used, plan_id=plan.id if plan else "",
                    )
                self._fail_plan(plan, reason)
                self._save_journal(plan, messages, pending_input=current_input, tool_batches=journal_batches)
                self._emit("runtime_status", category="error", status="failed", phase="model", runtime_mode=runtime_mode, iteration=i + 1, message=reason)
                self._emit("error", message=reason)
                return AgentRunResult(
                    "failed", "", model_used, messages, tools, system,
                    iterations=i + 1, latency_ms=total_latency,
                    fallback_used=fallback_used, plan_id=plan.id if plan else "",
                    error=reason,
                )
            model_response_received_at = time.monotonic()
            elapsed_ms = int((model_response_received_at - started) * 1000)
            usage = model_usage_metrics(result, elapsed_ms)
            performance.record_model(
                elapsed_ms, input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"], total_tokens=usage["total_tokens"],
            )
            if elapsed_ms >= 10_000 and not model_latency_warned:
                model_latency_warned = True
                self._emit(
                    "performance_warning",
                    kind="model_latency",
                    message="Model/API response latency is high.",
                    elapsed_ms=elapsed_ms,
                    model=active_model or "backend-default",
                )
            response = str(result.get("response", "") or "").strip()
            model_used = str(result.get("model", active_model or "?") or "?")
            latency = int(result.get("latency_ms") or elapsed_ms)
            total_latency += latency
            transport_telemetry = result.get("_transport_telemetry") if isinstance(result, dict) else None
            self._emit(
                "model_response", iteration=i + 1, elapsed_ms=elapsed_ms,
                model=model_used, requested=active_model or "backend-default", request_id=request_id,
                provider=(model_used.split("/", 1)[0] if "/" in model_used else ""),
                transport_telemetry=(transport_telemetry if isinstance(transport_telemetry, dict) else {}),
                **usage,
            )

            native_mode = self._native_tool_calling_enabled(active_model)
            if native_mode:
                native_calls = normalize_tool_calls(result.get("tool_calls") or [])
                text_calls = []
                recovered_calls = []
            else:
                native_calls = []
                text_calls = parse_tool_calls(
                    response,
                    allow_prose=bool(must_use_tools or tool_was_called or resumed),
                )
                recovered_calls = []
            calls = merge_tool_calls(native_calls, text_calls, recovered_calls)
            if native_mode:
                for call_index, call in enumerate(calls):
                    if not call.get("id"):
                        call["id"] = f"call_aicoder_{i + 1}_{call_index + 1}"
            visible = strip_tool_calls(response)

            if visible and calls:
                self._emit("thought", text=visible, iteration=i + 1)

            if not calls:
                protocol_expected = bool(must_use_tools or tool_was_called or resumed)
                unusable_final = (
                    (not response)
                    or _has_incomplete_tool_markup(response)
                    or (protocol_expected and _has_embedded_text_tool_protocol(response))
                )
                if unusable_final:
                    if response:
                        messages.append({"role": "assistant", "content": response})
                    if not final_response_repair_sent:
                        current_input = _FINAL_RESPONSE_REPAIR_PROMPT
                        final_response_repair_sent = True
                        self._emit(
                            "runtime_status", category="recovery", status="warning", phase="response_repair",
                            runtime_mode=runtime_mode, iteration=i + 1,
                            message="model response was unusable; requesting a clean final response",
                        )
                        self._emit(
                            "final_response_repair", iteration=i + 1,
                            reason=(
                                "empty_response" if not response
                                else "mixed_tool_protocol" if _has_embedded_text_tool_protocol(response)
                                else "incomplete_tool_call"
                            ),
                        )
                        self._save_journal(plan, messages, pending_input=current_input, tool_batches=journal_batches)
                        continue
                    reason = (
                        "Agent paused because the model returned no usable final response after "
                        "a final-response repair request. Existing tool results and plan state "
                        "were preserved for resume."
                    )
                    self._pause_plan(plan, reason, response)
                    self._save_journal(plan, messages, pending_input=reason, tool_batches=journal_batches)
                    self._emit("paused", reason=reason)
                    if self.conversation is not None:
                        self.conversation[:] = [dict(message) for message in messages[1:]][-MAX_CONTEXT_MESSAGES:]
                    return AgentRunResult(
                        "paused", reason, model_used, messages, tools, system,
                        iterations=i + 1, latency_ms=total_latency,
                        fallback_used=fallback_used, plan_id=plan.id if plan else "",
                    )

                if mutation_seen and not verification_seen:
                    messages.append({"role": "assistant", "content": response})
                    if not verification_nudge_sent:
                        current_input = _VERIFICATION_REQUIRED_PROMPT
                        verification_nudge_sent = True
                        self._emit(
                            "runtime_status", category="verify", status="required", phase="post_change",
                            runtime_mode=runtime_mode, iteration=i + 1,
                            message="workspace changed; successful post-change verification required before completion",
                        )
                        self._emit("verification_required", iteration=i + 1)
                        self._save_journal(plan, messages, pending_input=current_input, tool_batches=journal_batches)
                        continue
                    reason = (
                        "Agent paused: state changed successfully, but the model did not "
                        "perform a successful post-change verification after being prompted."
                    )
                    self._pause_plan(plan, reason, response)
                    self._save_journal(plan, messages, pending_input=current_input, tool_batches=journal_batches)
                    self._emit("paused", reason=reason)
                    if self.conversation is not None:
                        self.conversation[:] = [dict(message) for message in messages[1:]][-MAX_CONTEXT_MESSAGES:]
                    return AgentRunResult(
                        "paused", reason, model_used, messages, tools, system,
                        iterations=i + 1, latency_ms=total_latency,
                        fallback_used=fallback_used, plan_id=plan.id if plan else "",
                    )
                if must_use_tools and not tool_was_called and not tool_nudge_sent:
                    if response:
                        self._emit("thought", text=response, iteration=i + 1)
                    messages.append({"role": "assistant", "content": response})
                    current_input = (
                        "Continue the requested task now. No tool has been used yet. "
                        "Inspect the real local state with the most specific available tool, "
                        "then perform and verify the task. Do not only repeat a plan or ask "
                        "for generic confirmation. If execution is impossible, name the exact blocker."
                    )
                    tool_nudge_sent = True
                    self._save_journal(plan, messages, pending_input=current_input, tool_batches=journal_batches)
                    continue
                if mutation_seen and tool_was_called and not completion_audit_sent and _needs_completion_audit(self.initial_prompt):
                    messages.append({"role": "assistant", "content": response})
                    current_input = _completion_audit_prompt(self.initial_prompt)
                    completion_audit_sent = True
                    self._emit("completion_audit", iteration=i + 1)
                    self._save_journal(plan, messages, pending_input=current_input, tool_batches=journal_batches)
                    continue
                messages.append({"role": "assistant", "content": response})
                guarded = self._guard_completion(
                    plan, messages, tools, system, model_used=model_used, iterations=i + 1,
                    total_latency=total_latency, fallback_used=fallback_used, journal_batches=journal_batches,
                )
                if guarded is not None:
                    return guarded
                self._complete_plan(
                    plan, response,
                    mutation_seen=mutation_seen,
                    verification_seen=verification_seen,
                )
                perf = performance_snapshot()
                self._emit("performance_summary", **perf)
                self._emit(
                    "runtime_status", category="run", status="completed", phase="complete",
                    runtime_mode=runtime_mode, iteration=i + 1,
                    message=f"runtime completed successfully after {i + 1} iteration(s)",
                )
                self._emit(
                    "final", response=response, model=model_used,
                    iterations=i + 1, latency_ms=total_latency,
                    fallback_used=fallback_used, performance=perf,
                )
                if self.conversation is not None:
                    self.conversation[:] = [dict(message) for message in messages[1:]][-MAX_CONTEXT_MESSAGES:]
                return AgentRunResult(
                    "completed", response, model_used, messages, tools, system,
                    iterations=i + 1, latency_ms=total_latency,
                    fallback_used=fallback_used, plan_id=plan.id if plan else "",
                )

            consecutive_call_batches = loop_guard.observe_calls(calls)
            polling_read_only_repeat = (
                consecutive_call_batches <= 3
                and bool(_POLLING_INTENT_RE.search(self.initial_prompt))
                and all(
                    not assess_execution(
                        str(call.get("name") or ""),
                        call.get("arguments") if isinstance(call.get("arguments"), dict) else {},
                        destructive=False,
                    ).mutation
                    for call in calls
                )
            )
            if consecutive_call_batches >= 2 and not polling_read_only_repeat:
                messages.append({"role": "assistant", "content": response})
                if consecutive_call_batches == 2:
                    current_input = (
                        "Duplicate tool call blocked before execution: this exact tool operation "
                        "was already executed on the previous turn. Use the existing result, "
                        "inspect different evidence, change the arguments, or finish with a clear "
                        "answer/blocker. Do not repeat the same call unchanged."
                    )
                    self._emit(
                        "runtime_status", category="recovery", status="warning", phase="loop_guard",
                        runtime_mode=runtime_mode, iteration=i + 1,
                        message="duplicate tool operation blocked; model must change approach",
                    )
                    self._emit(
                        "loop_prevented", iteration=i + 1, repeats=consecutive_call_batches,
                        action="nudge",
                    )
                    self._save_journal(plan, messages, pending_input=current_input, tool_batches=journal_batches)
                    continue
                reason = (
                    "Agent paused because it kept requesting the same tool operation after that "
                    "duplicate had already been blocked. The previous tool result remains available; "
                    "resume after changing the approach."
                )
                self._pause_plan(plan, reason, response)
                self._save_journal(plan, messages, pending_input=reason, tool_batches=journal_batches)
                self._emit("paused", reason=reason)
                return AgentRunResult(
                    "paused", reason, model_used, messages, tools, system,
                    iterations=i + 1, latency_ms=total_latency,
                    fallback_used=fallback_used, plan_id=plan.id if plan else "",
                )

            tool_was_called = True
            tool_results: list[str] = []
            batch_mutation = False
            batch_verification = False
            native_tool_messages: list[dict[str, Any]] = []
            batch_records: list[dict[str, Any]] = []
            batch_failure_repeats = 0
            batch_failure_category = ""
            for call in calls:
                if self._stopped():
                    reason = "Agent stopped by user"
                    self._pause_plan(plan, reason, response)
                    self._save_journal(plan, messages, pending_input=current_input, tool_batches=journal_batches)
                    self._emit("paused", reason=reason)
                    return AgentRunResult(
                        "paused", reason, model_used, messages, tools, system,
                        iterations=i + 1, latency_ms=total_latency,
                        fallback_used=fallback_used, plan_id=plan.id if plan else "",
                    )

                name = str(call.get("name") or "?")
                args = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
                handoff_ms = int((time.monotonic() - model_response_received_at) * 1000)
                self._emit(
                    "tool_call", name=name, arguments=args, iteration=i + 1,
                    request_id=request_id, handoff_ms=handoff_ms,
                )
                if name in META_TOOL_NAMES:
                    started_tool = time.monotonic()
                    tool_result, is_error, tools_changed = self._run_meta_tool(name, args, tools)
                    elapsed = time.monotonic() - started_tool
                    if tools_changed:
                        allowed_tool_names = {
                            str(tool.get("name")) for tool in tools if tool.get("name")
                        }
                        base_system = self.system_prompt or build_system_prompt(tools, workspace)
                        protocol_system = self._system_for_tool_protocol(
                            base_system, native=self._native_tool_calling_enabled(active_model)
                        )
                        messages[0]["content"] = self._with_plan_context(protocol_system, plan)
                        system = messages[0]["content"]
                    performance.record_tool(name, elapsed, is_error=is_error)
                    if (
                        name in {"file_read", "file_edit", "file_tree", "code_search", "code_grep"}
                        and elapsed >= 2.0
                        and not filesystem_latency_warned
                    ):
                        filesystem_latency_warned = True
                        self._emit(
                            "performance_warning", kind="filesystem_latency",
                            message="A filesystem operation is unusually slow.",
                            elapsed_ms=int(elapsed * 1000), tool=name,
                        )
                    self._emit(
                        "tool_result", name=name, result=tool_result,
                        is_error=is_error, elapsed=elapsed, iteration=i + 1,
                        request_id=request_id, handoff_ms=handoff_ms,
                    )
                    tool_results.append(f"Tool {name} result:\n{tool_result}")
                    if native_mode:
                        native_tool_messages.append({
                            "role": "tool", "tool_call_id": str(call.get("id") or ""),
                            "name": name, "content": str(tool_result),
                        })
                    batch_records.append({
                        "id": str(call.get("id") or ""), "name": name,
                        "provider": str(call.get("provider") or ""),
                        "raw_type": str(call.get("raw_type") or ""),
                        "metadata": call.get("metadata") if isinstance(call.get("metadata"), dict) else {},
                        "arguments": args, "is_error": bool(is_error),
                    })
                    continue
                allowed, reason = require_allowed_tool(name, allowed_tool_names)
                risk = assess_execution(
                    name, args, destructive=is_destructive(str(args.get("command", "")))
                )
                pre_hook = self.hooks.emit("PreToolUse", {
                    "name": name, "arguments": dict(args), "workspace": workspace,
                    "iteration": i + 1, "risk": tuple(risk.reasons),
                })
                policy_status = "ok"
                policy_message = f"tool {name} allowed"
                if not allowed:
                    policy_status = "blocked"
                    policy_message = f"tool {name} blocked: {reason}"
                elif pre_hook.blocked:
                    policy_status = "blocked"
                    policy_message = f"tool {name} blocked by policy hook: {pre_hook.reason or 'denied'}"
                elif resumed and not fresh_inspection_after_resume and (risk.mutation or risk.destructive):
                    policy_status = "blocked"
                    policy_message = f"tool {name} blocked: resumed run requires fresh inspection before mutation"
                self._emit(
                    "runtime_status", category="policy", status=policy_status, phase="tool_policy",
                    runtime_mode=runtime_mode, iteration=i + 1, tool=name,
                    risk=list(risk.reasons), message=policy_message,
                )
                for diagnostic in pre_hook.diagnostics:
                    self._emit("hook_diagnostic", event="PreToolUse", message=diagnostic, tool=name)
                if not allowed:
                    tool_result, is_error = f"{name}: blocked — {reason}", True
                    elapsed = 0.0
                elif pre_hook.blocked:
                    tool_result = f"{name}: blocked by hook — {pre_hook.reason or 'policy hook denied operation'}"
                    is_error = True
                    elapsed = 0.0
                elif (
                    resumed
                    and not fresh_inspection_after_resume
                    and (risk.mutation or risk.destructive)
                ):
                    tool_result = (
                        f"{name}: blocked — resumed plans require a fresh successful read/check "
                        "of the current workspace before any new mutation"
                    )
                    is_error = True
                    elapsed = 0.0
                else:
                    started_tool = time.monotonic()
                    recall_key = None
                    if name == "file_read" and evidence_store is not None:
                        raw_path = str(args.get("path") or "")
                        evidence_path = Path(raw_path).expanduser()
                        if not evidence_path.is_absolute():
                            evidence_path = Path(workspace) / evidence_path
                        normalized_path = str(evidence_path.resolve(strict=False))
                        recall_key = (
                            normalized_path,
                            max(1, int(args.get("start_line") or 1)),
                            max(0, int(args.get("end_line") or 0)),
                        )
                        if recall_key in run_file_reads:
                            try:
                                _, unchanged = evidence_store.inspect_file(*recall_key, force_hash=True)
                            except TypeError:
                                # Compatibility for alternate/test evidence stores that predate
                                # the explicit forced-hash API.
                                _, unchanged = evidence_store.inspect_file(*recall_key)
                            except Exception:
                                unchanged = False
                            if unchanged:
                                tool_result = (
                                    "UNCHANGED EVIDENCE ALREADY AVAILABLE IN THIS RUN. "
                                    "Recall the prior file_read result or request a different range "
                                    "with a concrete reason."
                                )
                                is_error = False
                                elapsed = time.monotonic() - started_tool
                                self._emit("evidence_recall", path=raw_path, start_line=recall_key[1], end_line=recall_key[2])
                                tool_results.append(f"Tool {name} result:\n{tool_result}")
                                if native_mode:
                                    native_tool_messages.append({
                                        "role": "tool", "tool_call_id": str(call.get("id") or ""),
                                        "name": name, "content": str(tool_result),
                                    })
                                mutation_seen, verified_now = self._record_tool_progress(
                                    plan, name, args, tool_result, is_error, mutation_seen,
                                )
                                verification_seen = verification_seen or verified_now
                                batch_records.append({
                                    "id": str(call.get("id") or ""), "name": name,
                                    "provider": str(call.get("provider") or ""),
                                    "raw_type": str(call.get("raw_type") or ""),
                                    "metadata": call.get("metadata") if isinstance(call.get("metadata"), dict) else {},
                                    "arguments": args, "is_error": False,
                                })
                                continue
                    if name == "subagent_run":
                        from .subagents import run_subagent
                        self.hooks.emit("SubagentStart", {
                            "role": str(args.get("role") or "analyze"),
                            "task": str(args.get("task") or "")[:2000],
                            "workspace": workspace,
                        })
                        tool_result, is_error = run_subagent(
                            model_client,
                            task=str(args.get("task") or ""),
                            role=str(args.get("role") or "analyze"),
                            context=str(args.get("context") or ""),
                            model=active_model or model_used,
                            execution_client=self.client,
                            tools=[tool for tool in tools if tool.get("name") != "subagent_run"],
                            workspace_root=self.workspace_root,
                            protected_workspace_root=self.protected_workspace_root,
                            approval_fn=self.approval_fn,
                            enabled_tool_names=self.enabled_tool_names,
                            fallback_model=active_fallback,
                            stop_requested=self.stop_requested,
                        )
                        self.hooks.emit("SubagentStop", {
                            "role": str(args.get("role") or "analyze"),
                            "workspace": workspace, "is_error": bool(is_error),
                            "result": str(tool_result)[:2000],
                        })
                    else:
                        tool_result, is_error = run_tool(
                            self.client,
                            name,
                            args,
                            approval_fn=self.approval_fn,
                            model=model_used,
                            iteration=i,
                            allowed_tools=allowed_tool_names,
                            workspace_root=self.workspace_root,
                            protected_workspace_root=self.protected_workspace_root,
                        )
                    elapsed = time.monotonic() - started_tool
                if not is_error and name in _INSPECTION_TOOLS:
                    fresh_inspection_after_resume = True
                performance.record_tool(name, elapsed, is_error=is_error)
                if (
                    name in {"file_read", "file_edit", "file_tree", "code_search", "code_grep"}
                    and elapsed >= 2.0
                    and not filesystem_latency_warned
                ):
                    filesystem_latency_warned = True
                    self._emit(
                        "performance_warning", kind="filesystem_latency",
                        message="A filesystem operation is unusually slow.",
                        elapsed_ms=int(elapsed * 1000), tool=name,
                    )
                self._emit(
                    "tool_result", name=name, result=tool_result,
                    is_error=is_error, elapsed=elapsed, iteration=i + 1,
                    request_id=request_id, handoff_ms=handoff_ms,
                )
                hook_event = "PostToolUseFailure" if is_error else "PostToolUse"
                post_hook = self.hooks.emit(hook_event, {
                    "name": name, "arguments": dict(args), "workspace": workspace,
                    "iteration": i + 1, "result": str(tool_result)[:4000],
                })
                for diagnostic in post_hook.diagnostics:
                    self._emit("hook_diagnostic", event=hook_event, message=diagnostic, tool=name)
                tool_results.append(f"Tool {name} result:\n{tool_result}")
                if native_mode:
                    native_tool_messages.append({
                        "role": "tool", "tool_call_id": str(call.get("id") or ""),
                        "name": name, "content": str(tool_result),
                    })
                if not is_error and name == "file_read" and evidence_store is not None:
                    try:
                        raw_path = str(args.get("path") or "")
                        evidence_path = Path(raw_path).expanduser()
                        if not evidence_path.is_absolute():
                            evidence_path = Path(workspace) / evidence_path
                        normalized_path = str(evidence_path.resolve(strict=False))
                        key = (
                            normalized_path,
                            max(1, int(args.get("start_line") or 1)),
                            max(0, int(args.get("end_line") or 0)),
                        )
                        evidence_store.inspect_file(*key)
                        run_file_reads.add(key)
                    except Exception as exc:
                        self._emit("evidence_record_failed", evidence_kind="file", error=f"{type(exc).__name__}: {exc}")
                failure = failure_tracker.observe(tool_result, is_error)
                if failure is not None:
                    if evidence_store is not None:
                        try:
                            evidence_store.remember_failure(failure.category, failure.signature, failure.count)
                        except Exception as exc:
                            self._emit("evidence_record_failed", evidence_kind="failure", error=f"{type(exc).__name__}: {exc}")
                    if failure.count > batch_failure_repeats:
                        batch_failure_repeats = failure.count
                        batch_failure_category = failure.category
                if is_error and str(tool_result).strip().endswith(": aborted by user"):
                    reason = f"Agent paused because the user rejected {name}."
                    if native_mode:
                        processed_calls = calls[:len(native_tool_messages)]
                        messages.append({
                            "role": "assistant", "content": response or None,
                            "tool_calls": [
                                {
                                    "id": str(item.get("id") or ""), "type": "function",
                                    "function": {
                                        "name": str(item.get("name") or ""),
                                        "arguments": json.dumps(item.get("arguments") or {}, ensure_ascii=False),
                                    },
                                }
                                for item in processed_calls
                            ],
                        })
                        messages.extend(native_tool_messages)
                    else:
                        messages.append({"role": "assistant", "content": response})
                        messages.append({"role": "user", "content": format_untrusted_tool_results(tool_results)})
                    self._pause_plan(plan, reason, response)
                    self._save_journal(plan, messages, tool_batches=journal_batches)
                    self._emit("paused", reason=reason)
                    return AgentRunResult(
                        "paused", reason, model_used, messages, tools, system,
                        iterations=i + 1, latency_ms=total_latency,
                        fallback_used=fallback_used, plan_id=plan.id if plan else "",
                    )
                mutation_seen, verified_now = self._record_tool_progress(
                    plan, name, args, tool_result, is_error, mutation_seen,
                )
                verification_seen = verification_seen or verified_now
                if not is_error and assess_execution(name, args, destructive=False).mutation:
                    batch_mutation = True
                batch_verification = batch_verification or verified_now
                if verified_now:
                    self._emit(
                        "runtime_status", category="verify", status="ok", phase="post_change",
                        runtime_mode=runtime_mode, iteration=i + 1, tool=name,
                        message=f"post-change verification satisfied by {name}",
                    )
                batch_records.append({
                    "id": str(call.get("id") or ""),
                    "name": name,
                    "provider": str(call.get("provider") or ""),
                    "raw_type": str(call.get("raw_type") or ""),
                    "metadata": call.get("metadata") if isinstance(call.get("metadata"), dict) else {},
                    "arguments": args,
                    "is_error": bool(is_error),
                })

            if batch_records:
                journal_batches.append({
                    "iteration": starting_plan_iteration + i + 1,
                    "calls": batch_records,
                })
                journal_batches = journal_batches[-20:]
                if batch_mutation or batch_verification:
                    read_only_batch_streak = 0
                elif any(not bool(record.get("is_error")) for record in batch_records):
                    read_only_batch_streak += 1
                if read_only_batch_streak == READ_ONLY_PROGRESS_NUDGE_BATCHES:
                    tool_results.append(
                        "Progress guard: several consecutive read-only tool batches completed without a mutation "
                        "or verification milestone. Consolidate the evidence already collected before reading more. "
                        "Continue inspection only for a specific unresolved requirement; otherwise implement the "
                        "smallest justified change, verify it, or finish with a concrete no-change conclusion."
                    )
                    self._emit(
                        "runtime_status", category="recovery", status="warning", phase="progress_guard",
                        runtime_mode=runtime_mode, iteration=i + 1,
                        message=f"read-only progress guard after {read_only_batch_streak} consecutive batches",
                    )

            base_tool_result_count = len(tool_results)
            repeats = loop_guard.observe(calls, tool_results)
            all_failed = bool(batch_records) and all(record.get("is_error") for record in batch_records)
            research_tools = {
                name for name in ("memory_search", "search", "crawl", "web_fetch_local")
                if name in allowed_tool_names
            }
            if batch_failure_category == "persistent_dependency" and batch_failure_repeats >= 3:
                tool_results.append(
                    "Dependency circuit open: the same transient dependency failure exceeded "
                    "its retry budget. Do not call the same failing dependency again in this "
                    "run unless new evidence indicates recovery. Use an alternative provider/tool, "
                    "continue with independent local evidence, or report the blocker."
                )
                self._emit(
                    "failure_circuit_open", iteration=i + 1, category=batch_failure_category,
                    repeats=batch_failure_repeats,
                )
            elif batch_failure_repeats == 2:
                tool_results.append(
                    REPEATED_ERROR_RECOVERY_PROMPT
                    + f" Failure category: {batch_failure_category}. The same underlying failure "
                    "has recurred even if the surrounding tool call changed."
                )
                self._emit(
                    "failure_replan", iteration=i + 1, category=batch_failure_category,
                    repeats=batch_failure_repeats,
                )
            elif batch_failure_repeats >= 3 and research_tools & {"search", "crawl", "web_fetch_local"}:
                tool_results.append(RESEARCH_RECOVERY_PROMPT)
                self._emit(
                    "research_recovery", iteration=i + 1, tools=sorted(research_tools),
                    category=batch_failure_category,
                )
            elif all_failed and repeats == 2:
                tool_results.append(REPEATED_ERROR_RECOVERY_PROMPT)
            elif repeats == STALL_NUDGE_REPEATS:
                if research_tools & {"search", "crawl", "web_fetch_local"}:
                    tool_results.append(RESEARCH_RECOVERY_PROMPT)
                    self._emit(
                        "research_recovery",
                        iteration=i + 1,
                        tools=sorted(research_tools),
                    )
                else:
                    tool_results.append(STALL_RECOVERY_PROMPT)

            if repeats >= STALL_FALLBACK_REPEATS:
                reason = (
                    "Agent paused because the same tool operation kept repeating without "
                    "progress. Resume the persistent plan with 'continue' after correcting "
                    "the approach or environment."
                )
                if native_mode:
                    messages.append({
                        "role": "assistant", "content": response or None,
                        "tool_calls": [
                            {
                                "id": str(item.get("id") or ""), "type": "function",
                                "function": {
                                    "name": str(item.get("name") or ""),
                                    "arguments": json.dumps(item.get("arguments") or {}, ensure_ascii=False),
                                },
                            }
                            for item in calls
                        ],
                    })
                    messages.extend(native_tool_messages)
                else:
                    messages.append({"role": "assistant", "content": response})
                    messages.append({"role": "user", "content": format_untrusted_tool_results(tool_results)})
                self._pause_plan(plan, reason, response)
                self._save_journal(plan, messages, tool_batches=journal_batches)
                self._emit("paused", reason=reason)
                if self.conversation is not None:
                    self.conversation[:] = [dict(message) for message in messages[1:]][-MAX_CONTEXT_MESSAGES:]
                return AgentRunResult(
                    "paused", reason, model_used, messages, tools, system,
                    iterations=i + 1, latency_ms=total_latency,
                    fallback_used=fallback_used, plan_id=plan.id if plan else "",
                )

            if (i + 1) % AGENT_CHECKPOINT_INTERVAL == 0:
                tool_results.append(agent_checkpoint(i + 1))

            if native_mode:
                assistant_tool_calls = [
                    {
                        "id": str(call.get("id") or ""),
                        "type": "function",
                        "function": {
                            "name": str(call.get("name") or ""),
                            "arguments": json.dumps(call.get("arguments") or {}, ensure_ascii=False),
                        },
                    }
                    for call in calls
                ]
                messages.append({
                    "role": "assistant",
                    "content": response or None,
                    "tool_calls": assistant_tool_calls,
                })
                messages.extend(native_tool_messages)
                followup_notes = tool_results[base_tool_result_count:]
                current_input = "\n\n".join(followup_notes)
            else:
                messages.append({"role": "assistant", "content": response})
                current_input = format_untrusted_tool_results(tool_results)
            self._save_journal(plan, messages, tool_batches=journal_batches)

            if response.strip().upper().startswith("DONE:"):
                if mutation_seen and not verification_seen:
                    if not verification_nudge_sent:
                        current_input += "\n\n" + _VERIFICATION_REQUIRED_PROMPT
                        verification_nudge_sent = True
                        self._emit(
                            "runtime_status", category="verify", status="required", phase="post_change",
                            runtime_mode=runtime_mode, iteration=i + 1,
                            message="workspace changed; successful post-change verification required before completion",
                        )
                        self._emit("verification_required", iteration=i + 1)
                        continue
                    reason = (
                        "Agent paused: state changed successfully, but DONE was requested "
                        "without a successful post-change verification."
                    )
                    self._pause_plan(plan, reason, visible or response)
                    self._save_journal(plan, messages, tool_batches=journal_batches)
                    self._emit("paused", reason=reason)
                    if self.conversation is not None:
                        self.conversation[:] = [dict(message) for message in messages[1:]][-MAX_CONTEXT_MESSAGES:]
                    return AgentRunResult(
                        "paused", reason, model_used, messages, tools, system,
                        iterations=i + 1, latency_ms=total_latency,
                        fallback_used=fallback_used, plan_id=plan.id if plan else "",
                    )
                if (
                    mutation_seen
                    and tool_was_called
                    and not completion_audit_sent
                    and _needs_completion_audit(self.initial_prompt)
                ):
                    current_input += "\n\n" + _completion_audit_prompt(self.initial_prompt)
                    completion_audit_sent = True
                    self._emit("completion_audit", iteration=i + 1)
                    self._save_journal(
                        plan, messages, pending_input=current_input, tool_batches=journal_batches
                    )
                    continue
                guarded = self._guard_completion(
                    plan, messages, tools, system, model_used=model_used, iterations=i + 1,
                    total_latency=total_latency, fallback_used=fallback_used, journal_batches=journal_batches,
                )
                if guarded is not None:
                    return guarded
                self._complete_plan(
                    plan, visible or response,
                    mutation_seen=mutation_seen,
                    verification_seen=verification_seen,
                )
                perf = performance_snapshot()
                self._emit("performance_summary", **perf)
                self._emit(
                    "runtime_status", category="run", status="completed", phase="complete",
                    runtime_mode=runtime_mode, iteration=i + 1,
                    message=f"runtime completed successfully after {i + 1} iteration(s)",
                )
                self._emit(
                    "final", response=visible or response, model=model_used,
                    iterations=i + 1, latency_ms=total_latency,
                    fallback_used=fallback_used, performance=perf,
                )
                if self.conversation is not None:
                    self.conversation[:] = [dict(message) for message in messages[1:]][-MAX_CONTEXT_MESSAGES:]
                return AgentRunResult(
                    "completed", visible or response, model_used, messages, tools, system,
                    iterations=i + 1, latency_ms=total_latency,
                    fallback_used=fallback_used, plan_id=plan.id if plan else "",
                )

        if current_input:
            messages.append({"role": "user", "content": current_input})
        reason = (
            "Agent safety pause after an unusually long run. The persistent plan is "
            "preserved; resume it with 'continue'."
        )
        self._pause_plan(plan, reason)
        self._save_journal(plan, messages, tool_batches=journal_batches)
        self._emit("paused", reason=reason)
        if self.conversation is not None:
            self.conversation[:] = [dict(message) for message in messages[1:]][-MAX_CONTEXT_MESSAGES:]
        return AgentRunResult(
            "paused", reason, model_used, messages, tools, system,
            iterations=iteration_limit, latency_ms=total_latency,
            fallback_used=fallback_used, plan_id=plan.id if plan else "",
        )
