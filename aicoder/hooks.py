"""Deterministic in-process lifecycle hooks for AICoder.

Hooks may observe, add bounded context/metadata, or block an operation. They are
never an authorization mechanism and cannot mark an operation approved, widen a
sandbox, or bypass the PrivilegeBroker/tool policy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

HOOK_EVENTS = frozenset({
    "SessionStart", "SessionEnd",
    "PreToolUse", "PostToolUse", "PostToolUseFailure",
    "SubagentStart", "SubagentStop",
})
_SECURITY_PRE_EVENTS = frozenset({"PreToolUse"})
MAX_HOOK_CONTEXT_CHARS = 4000


@dataclass(frozen=True)
class HookDecision:
    blocked: bool = False
    reason: str = ""
    context: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()


HookHandler = Callable[[dict[str, Any]], HookDecision | dict[str, Any] | None]


@dataclass
class HookBus:
    _handlers: dict[str, list[HookHandler]] = field(default_factory=dict)

    def register(self, event: str, handler: HookHandler) -> None:
        name = str(event or "")
        if name not in HOOK_EVENTS:
            raise ValueError(f"unsupported hook event: {name}")
        if not callable(handler):
            raise TypeError("hook handler must be callable")
        self._handlers.setdefault(name, []).append(handler)

    def emit(self, event: str, payload: dict[str, Any] | None = None) -> HookDecision:
        name = str(event or "")
        if name not in HOOK_EVENTS:
            raise ValueError(f"unsupported hook event: {name}")
        data = dict(payload or {})
        blocked = False
        reasons: list[str] = []
        context: list[str] = []
        diagnostics: list[str] = []
        for handler in tuple(self._handlers.get(name, ())):
            try:
                raw = handler(dict(data))
                if raw is None:
                    continue
                if isinstance(raw, HookDecision):
                    decision = raw
                elif isinstance(raw, dict):
                    raw_context = raw.get("context")
                    if isinstance(raw_context, str):
                        ctx = (raw_context,)
                    elif isinstance(raw_context, (list, tuple)):
                        ctx = tuple(str(item) for item in raw_context)
                    else:
                        ctx = ()
                    decision = HookDecision(
                        blocked=bool(raw.get("blocked")),
                        reason=str(raw.get("reason") or ""),
                        context=ctx,
                    )
                else:
                    diagnostics.append(f"{name}: ignored invalid hook result {type(raw).__name__}")
                    continue
                if decision.blocked:
                    blocked = True
                    if decision.reason:
                        reasons.append(decision.reason[:1000])
                for item in decision.context:
                    remaining = MAX_HOOK_CONTEXT_CHARS - sum(len(x) for x in context)
                    if remaining <= 0:
                        break
                    text = str(item)[:remaining]
                    if text:
                        context.append(text)
                diagnostics.extend(str(item)[:1000] for item in decision.diagnostics)
            except Exception as exc:
                message = f"{name}: hook {getattr(handler, '__name__', type(handler).__name__)} failed: {type(exc).__name__}: {exc}"
                diagnostics.append(message[:1000])
                if name in _SECURITY_PRE_EVENTS:
                    blocked = True
                    reasons.append("security pre-hook failed closed")
        return HookDecision(
            blocked=blocked,
            reason="; ".join(dict.fromkeys(reasons))[:2000],
            context=tuple(context),
            diagnostics=tuple(diagnostics),
        )
