"""Opt-in native-light agent runtime shared by CLI and GUI.

This module owns the agent state machine. Presentation stays in callers through
runtime events, so future skills/subagents can extend one loop instead of two.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .agent_journal import ContinuationJournalStore
from .agent_plan import AgentPlan, PlanStore, plan_prompt_context, resume_prompt_context
from .client import ClientError, TriForceClient
from .executor import (
    AGENT_CHECKPOINT_INTERVAL,
    MAX_CONTEXT_MESSAGES,
    MAX_ITERATIONS,
    STALL_FALLBACK_REPEATS,
    STALL_NUDGE_REPEATS,
    STALL_RECOVERY_PROMPT,
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
ApprovalFn = Callable[[str, dict], bool]
StopFn = Callable[[], bool]

_VERIFY_TOOLS = {
    "test", "lint", "git", "file_read", "code_grep",
    "code_read", "code_search", "dev_lint", "dev_analyze",
}
_INSPECTION_TOOLS = _VERIFY_TOOLS | {"file_tree", "code_tree"}

_VERIFICATION_REQUIRED_PROMPT = (
    "Verification required: a state-changing tool succeeded, but no successful "
    "post-change verification has been observed. Inspect the resulting state with "
    "a read/check/lint/test tool now. Do not report DONE until verification succeeds."
)


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
    fallback_model: str | None
    workspace_root: str
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

    def _emit(self, kind: str, **payload: Any) -> None:
        if self.event_fn is not None:
            self.event_fn(kind, payload)

    def _stopped(self) -> bool:
        return bool(self.stop_requested and self.stop_requested())

    def _prepare_tools(self) -> list[dict]:
        if self.tools is None and self.load_tools_on_start:
            started = time.monotonic()
            tools = load_tools(self.client)
            if self.enabled_tool_names is not None:
                enabled = set(self.enabled_tool_names)
                tools = [tool for tool in tools if tool.get("name") in enabled]
            self.tools = tools
            self._emit("tools_ready", count=len(tools), elapsed=time.monotonic() - started)
        elif self.tools is None:
            self.tools = []
        return self.tools

    def _prepare_plan(self) -> tuple[AgentPlan | None, bool]:
        if not self.persistent_plan:
            return None, False
        if self.quick_chat and not self.resume:
            return None, False
        plan: AgentPlan | None = None
        if self.resume:
            if self.resume_plan_id == "current":
                plan = self.plan_store.load_current(self.workspace_root)
                if plan is None:
                    raise ValueError("no current persistent plan to resume in this workspace")
            elif self.resume_plan_id:
                plan = self.plan_store.load(self.workspace_root, self.resume_plan_id)
                if plan is None:
                    raise ValueError(f"resume plan not found in this workspace: {self.resume_plan_id}")
            else:
                plan = self.plan_store.load_current(self.workspace_root)
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
            self.initial_prompt, self.workspace_root, str(self.model or "")
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
        if plan is None:
            return mutation_seen, False
        risk = assess_execution(name, args, destructive=is_destructive(str(args.get("command", ""))))
        # Persist only execution metadata, never raw tool output. Tool results may
        # contain source snippets, tokens, credentials, or other sensitive data.
        plan.record_event(
            "tool",
            f"{name} {'failed' if is_error else 'completed'}",
            tool=name,
            is_error=is_error,
        )
        if is_error:
            self._save_plan(plan)
            return mutation_seen, False

        verification_seen = False
        if name in _INSPECTION_TOOLS:
            # Security classification is intentionally conservative: lint/test
            # may create caches and therefore require approval. Plan semantics
            # treat them as verification/inspection rather than implementation.
            if mutation_seen:
                plan.set_step("verify", "completed", f"Verified via {name}")
                verification_seen = True
            else:
                plan.set_step("inspect", "completed", f"Checked state via {name}")
                plan.set_step("implement", "skipped", "No state mutation required")
                plan.set_step("verify", "completed", f"Verification task completed via {name}")
                verification_seen = True
        elif risk.mutation or risk.destructive:
            mutation_seen = True
            plan.set_step("inspect", "completed", "Relevant state inspected before mutation")
            plan.set_step("implement", "completed", f"Successful mutation via {name}")
            plan.set_step("verify", "in_progress", "Waiting for post-change verification")
        else:
            inspect = next((step for step in plan.steps if step.id == "inspect"), None)
            if inspect is not None and inspect.status == "in_progress":
                plan.set_step("inspect", "completed", f"Successful inspection via {name}")
                plan.set_step("implement", "in_progress", "Inspection completed")
        self._save_plan(plan)
        return mutation_seen, verification_seen

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
        workspace = str(Path(self.workspace_root or ".").resolve())
        self.workspace_root = workspace
        tools = self._prepare_tools()
        try:
            plan, resumed = self._prepare_plan()
        except ValueError as exc:
            reason = str(exc)
            self._emit("error", message=reason)
            return AgentRunResult(
                "failed", "", str(self.model or "?"), [], tools, "",
                error=reason,
            )
        base_system = self.system_prompt or build_system_prompt(tools, workspace)
        system = self._with_plan_context(base_system, plan)

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
        active_fallback = self.fallback_model
        model_used = active_model or "?"
        total_latency = 0
        fallback_used = False
        tool_was_called = False
        tool_nudge_sent = False
        mutation_seen, verification_seen = plan.progress_flags() if resumed and plan else (False, False)
        verification_nudge_sent = False
        fresh_inspection_after_resume = not resumed
        starting_plan_iteration = plan.iteration if plan is not None else 0
        loop_guard = AgentLoopGuard()

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

        self._emit(
            "run_start",
            model=active_model or "backend-default",
            fallback=active_fallback or "",
            tools=len(tools),
            workspace=workspace,
            plan_id=plan.id if plan else "",
            resumed=resumed,
        )

        for i in range(MAX_ITERATIONS):
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
                messages[0]["content"] = self._with_plan_context(base_system, plan)
                system = messages[0]["content"]

            messages.append({"role": "user", "content": current_input})
            messages = trim_messages(messages)
            timeout = adaptive_request_timeout(
                self.base_timeout,
                prompt=self.initial_prompt,
                iteration=i,
                quick_chat=self.quick_chat,
                model=active_model,
            )
            self._emit("model_start", iteration=i + 1, timeout=timeout, model=active_model or "")
            started = time.monotonic()
            try:
                result = chat_with_timeout(
                    model_client,
                    timeout,
                    messages=messages,
                    model=active_model,
                    fallback_model=active_fallback,
                    temperature=0.3,
                    max_tokens=256 if self.quick_chat else 4096,
                    tools=tools if self.load_tools_on_start else None,
                    tool_choice="auto",
                )
            except (ClientError, RuntimeError) as exc:
                reason = str(exc)
                self._fail_plan(plan, reason)
                self._save_journal(plan, messages, pending_input=current_input, tool_batches=journal_batches)
                self._emit("error", message=reason)
                return AgentRunResult(
                    "failed", "", model_used, messages, tools, system,
                    iterations=i + 1, latency_ms=total_latency,
                    fallback_used=fallback_used, plan_id=plan.id if plan else "",
                    error=reason,
                )
            elapsed_ms = int((time.monotonic() - started) * 1000)
            response = str(result.get("response", "") or "").strip()
            model_used = str(result.get("model", active_model or "?") or "?")
            latency = int(result.get("latency_ms") or elapsed_ms)
            total_latency += latency
            if result.get("fallback_used"):
                fallback_used = True
            self._emit(
                "model_response", iteration=i + 1, elapsed_ms=elapsed_ms,
                model=model_used, requested=active_model or "backend-default",
            )

            native_calls = normalize_tool_calls(result.get("tool_calls") or [])
            calls = merge_tool_calls(native_calls, parse_tool_calls(response))
            if native_calls and not response:
                response = "\n".join(
                    f"<tool_call>{json.dumps(call, ensure_ascii=False)}</tool_call>"
                    for call in native_calls
                )
            visible = strip_tool_calls(response)

            if visible and calls:
                self._emit("thought", text=visible, iteration=i + 1)

            if not calls:
                if mutation_seen and not verification_seen:
                    messages.append({"role": "assistant", "content": response})
                    if not verification_nudge_sent:
                        current_input = _VERIFICATION_REQUIRED_PROMPT
                        verification_nudge_sent = True
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
                messages.append({"role": "assistant", "content": response})
                self._complete_plan(
                    plan, response,
                    mutation_seen=mutation_seen,
                    verification_seen=verification_seen,
                )
                self._emit(
                    "final", response=response, model=model_used,
                    iterations=i + 1, latency_ms=total_latency,
                    fallback_used=fallback_used,
                )
                if self.conversation is not None:
                    self.conversation[:] = [dict(message) for message in messages[1:]][-MAX_CONTEXT_MESSAGES:]
                return AgentRunResult(
                    "completed", response, model_used, messages, tools, system,
                    iterations=i + 1, latency_ms=total_latency,
                    fallback_used=fallback_used, plan_id=plan.id if plan else "",
                )

            tool_was_called = True
            tool_results: list[str] = []
            batch_records: list[dict[str, Any]] = []
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
                self._emit("tool_call", name=name, arguments=args, iteration=i + 1)
                allowed, reason = require_allowed_tool(name, allowed_tool_names)
                risk = assess_execution(
                    name, args, destructive=is_destructive(str(args.get("command", "")))
                )
                if not allowed:
                    tool_result, is_error = f"{name}: blocked — {reason}", True
                    elapsed = 0.0
                elif (
                    resumed
                    and not fresh_inspection_after_resume
                    and name not in _VERIFY_TOOLS
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
                    if name == "subagent_run":
                        from .subagents import run_subagent
                        tool_result, is_error = run_subagent(
                            model_client,
                            task=str(args.get("task") or ""),
                            role=str(args.get("role") or "analyze"),
                            context=str(args.get("context") or ""),
                            model=active_model or model_used,
                        )
                    else:
                        tool_result, is_error = run_tool(
                            self.client,
                            name,
                            args,
                            approval_fn=self.approval_fn,
                            model=model_used,
                            iteration=i,
                            allowed_tools=allowed_tool_names,
                        )
                    elapsed = time.monotonic() - started_tool
                if not is_error and name in _INSPECTION_TOOLS:
                    fresh_inspection_after_resume = True
                self._emit(
                    "tool_result", name=name, result=tool_result,
                    is_error=is_error, elapsed=elapsed,
                )
                tool_results.append(f"Tool {name} result:\n{tool_result}")
                mutation_seen, verified_now = self._record_tool_progress(
                    plan, name, args, tool_result, is_error, mutation_seen,
                )
                verification_seen = verification_seen or verified_now
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

            repeats = loop_guard.observe(calls, tool_results)
            if repeats == STALL_NUDGE_REPEATS:
                tool_results.append(STALL_RECOVERY_PROMPT)

            if repeats >= STALL_FALLBACK_REPEATS:
                if active_fallback and active_fallback != model_used:
                    self._emit(
                        "model_switch", previous=model_used, model=active_fallback,
                        reason="repeated tool loop",
                    )
                    tool_results.append(
                        f"Loop recovery: switch to {active_fallback} and continue the task "
                        "with a different approach. Do not repeat the prior call."
                    )
                    active_model = active_fallback
                    active_fallback = None
                    fallback_used = True
                    loop_guard.reset()
                else:
                    reason = (
                        "Agent paused because the same tool operation kept repeating without "
                        "progress. Resume the persistent plan with 'continue' after correcting "
                        "the approach or environment."
                    )
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

            messages.append({"role": "assistant", "content": response})
            current_input = format_untrusted_tool_results(tool_results)
            self._save_journal(plan, messages, tool_batches=journal_batches)

            if response.strip().upper().startswith("DONE:"):
                if mutation_seen and not verification_seen:
                    if not verification_nudge_sent:
                        current_input += "\n\n" + _VERIFICATION_REQUIRED_PROMPT
                        verification_nudge_sent = True
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
                self._complete_plan(
                    plan, visible or response,
                    mutation_seen=mutation_seen,
                    verification_seen=verification_seen,
                )
                self._emit(
                    "final", response=visible or response, model=model_used,
                    iterations=i + 1, latency_ms=total_latency,
                    fallback_used=fallback_used,
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
            iterations=MAX_ITERATIONS, latency_ms=total_latency,
            fallback_used=fallback_used, plan_id=plan.id if plan else "",
        )
