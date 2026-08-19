"""Persistent execution plans for the opt-in native-light agent runtime."""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import CONFIG_DIR, atomic_write_private

PLAN_SCHEMA_VERSION = 2
PLAN_RUNTIME = "native-light"
_PLAN_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_plan_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def _workspace_key(workspace: str) -> str:
    resolved = str(Path(workspace or ".").expanduser().resolve(strict=False))
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
    return digest


@dataclass
class PlanStep:
    id: str
    title: str
    status: str = "pending"
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlanStep":
        return cls(
            id=str(data.get("id") or "step"),
            title=str(data.get("title") or "Unnamed step"),
            status=str(data.get("status") or "pending"),
            detail=str(data.get("detail") or ""),
        )


@dataclass
class AgentPlan:
    id: str
    task: str
    workspace: str
    model: str = ""
    runtime: str = PLAN_RUNTIME
    status: str = "running"
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    iteration: int = 0
    resume_count: int = 0
    steps: list[PlanStep] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    last_response: str = ""
    pause_reason: str = ""
    schema_version: int = PLAN_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "runtime": self.runtime,
            "id": self.id,
            "task": self.task,
            "workspace": self.workspace,
            "model": self.model,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "iteration": self.iteration,
            "resume_count": self.resume_count,
            "steps": [step.to_dict() for step in self.steps],
            "events": list(self.events),
            "last_response": self.last_response,
            "pause_reason": self.pause_reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentPlan":
        raw_steps = data.get("steps") if isinstance(data.get("steps"), list) else []
        raw_events = data.get("events") if isinstance(data.get("events"), list) else []
        return cls(
            schema_version=int(data.get("schema_version") or PLAN_SCHEMA_VERSION),
            runtime=str(data.get("runtime") or PLAN_RUNTIME),
            id=str(data.get("id") or _new_plan_id()),
            task=str(data.get("task") or ""),
            workspace=str(data.get("workspace") or "."),
            model=str(data.get("model") or ""),
            status=str(data.get("status") or "running"),
            created_at=str(data.get("created_at") or _now()),
            updated_at=str(data.get("updated_at") or _now()),
            iteration=max(0, int(data.get("iteration") or 0)),
            resume_count=max(0, int(data.get("resume_count") or 0)),
            steps=[PlanStep.from_dict(item) for item in raw_steps if isinstance(item, dict)],
            events=[dict(item) for item in raw_events if isinstance(item, dict)][-80:],
            last_response=str(data.get("last_response") or ""),
            pause_reason=str(data.get("pause_reason") or ""),
        )

    def touch(self) -> None:
        self.updated_at = _now()

    def record_event(
        self,
        kind: str,
        message: str,
        *,
        tool: str = "",
        is_error: bool = False,
    ) -> None:
        self.events.append({
            "at": _now(),
            "kind": str(kind),
            "message": str(message)[:1000],
            "tool": str(tool),
            "is_error": bool(is_error),
        })
        self.events = self.events[-80:]
        self.touch()

    def set_step(self, step_id: str, status: str, detail: str = "") -> None:
        for step in self.steps:
            if step.id == step_id:
                step.status = status
                if detail:
                    step.detail = detail[:1000]
                self.touch()
                return

    def progress_flags(self) -> tuple[bool, bool]:
        """Restore safety-relevant execution progress without raw tool outputs."""
        statuses = {step.id: step.status for step in self.steps}
        mutation_seen = statuses.get("implement") == "completed"
        verification_seen = mutation_seen and statuses.get("verify") == "completed"
        return mutation_seen, verification_seen


class PlanStore:
    """Private per-workspace plan persistence with a stable current-plan pointer."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else CONFIG_DIR / "plans"

    def _workspace_dir(self, workspace: str) -> Path:
        path = self.root / _workspace_key(workspace)
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
            os.chmod(path, 0o700)
        except OSError:
            pass
        return path

    def _plan_path(self, workspace: str, plan_id: str) -> Path:
        if not _PLAN_ID_RE.fullmatch(plan_id):
            raise ValueError("invalid plan id")
        return self._workspace_dir(workspace) / f"plan-{plan_id}.json"

    def create(
        self,
        task: str,
        workspace: str,
        model: str = "",
        *,
        plan_id: str | None = None,
        runtime: str = PLAN_RUNTIME,
    ) -> AgentPlan:
        resolved_workspace = str(Path(workspace or ".").expanduser().resolve(strict=False))
        chosen_id = str(plan_id or _new_plan_id())
        if not _PLAN_ID_RE.fullmatch(chosen_id):
            raise ValueError("invalid plan id")
        plan = AgentPlan(
            id=chosen_id,
            task=str(task)[:4000],
            workspace=resolved_workspace,
            model=str(model or ""),
            runtime=str(runtime or PLAN_RUNTIME),
            steps=[
                PlanStep("inspect", "Inspect relevant state and establish the cause", "in_progress"),
                PlanStep("implement", "Perform the smallest effective implementation"),
                PlanStep("verify", "Verify behavior with checks/tests and report the result"),
            ],
        )
        plan.record_event("plan", "Plan created")
        self.save(plan)
        return plan

    def save(self, plan: AgentPlan) -> Path:
        plan.touch()
        payload = json.dumps(plan.to_dict(), ensure_ascii=False, indent=2)
        plan_path = self._plan_path(plan.workspace, plan.id)
        current_path = self._workspace_dir(plan.workspace) / "current.json"
        atomic_write_private(plan_path, payload)
        atomic_write_private(current_path, payload)
        return plan_path

    def load(self, workspace: str, plan_id: str) -> AgentPlan | None:
        path = self._plan_path(workspace, plan_id)
        return self._read(path)

    def load_current(self, workspace: str) -> AgentPlan | None:
        path = self._workspace_dir(workspace) / "current.json"
        return self._read(path)

    def list(self, workspace: str, limit: int = 20) -> list[AgentPlan]:
        directory = self._workspace_dir(workspace)
        rows: list[AgentPlan] = []
        for path in sorted(directory.glob("plan-*.json"), key=lambda p: p.stat().st_mtime_ns, reverse=True):
            plan = self._read(path)
            if plan is not None:
                rows.append(plan)
            if len(rows) >= max(1, min(100, int(limit))):
                break
        return rows

    def clear_current(self, workspace: str) -> bool:
        path = self._workspace_dir(workspace) / "current.json"
        if not path.exists():
            return False
        path.unlink()
        return True

    @staticmethod
    def _read(path: Path) -> AgentPlan | None:
        if not path.exists() or not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return None
            return AgentPlan.from_dict(data)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None


def format_plan(plan: AgentPlan) -> str:
    symbols = {
        "pending": "○",
        "in_progress": "◐",
        "completed": "✓",
        "failed": "✗",
        "skipped": "–",
    }
    lines = [
        f"plan={plan.id}  runtime={plan.runtime}  status={plan.status}  iteration={plan.iteration}",
        f"workspace={plan.workspace}",
        f"task={plan.task[:240]}",
    ]
    for step in plan.steps:
        symbol = symbols.get(step.status, "?")
        detail = f" — {step.detail}" if step.detail else ""
        lines.append(f"  {symbol} {step.id}: {step.title}{detail}")
    if plan.pause_reason:
        lines.append(f"pause={plan.pause_reason}")
    return "\n".join(lines)


def _recent_event_summary(plan: AgentPlan, limit: int = 8) -> str:
    rows: list[str] = []
    for event in plan.events[-max(1, min(20, int(limit))):]:
        kind = str(event.get("kind") or "event")
        tool = str(event.get("tool") or "")
        state = "error" if event.get("is_error") else "ok"
        label = f"{kind}:{tool}" if tool else kind
        rows.append(f"- {label} ({state})")
    return "\n".join(rows) if rows else "- no recorded execution events"


def plan_prompt_context(plan: AgentPlan) -> str:
    steps = "\n".join(
        f"- [{step.status}] {step.id}: {step.title}"
        for step in plan.steps
    )
    pause = f"Pause reason: {plan.pause_reason}\n" if plan.pause_reason else ""
    return (
        "## Native-light persistent execution plan\n"
        f"Plan ID: {plan.id}\n"
        f"Original task: {plan.task[:2000]}\n"
        f"Status: {plan.status}\n"
        f"Iteration: {plan.iteration}\n"
        f"Resume count: {plan.resume_count}\n"
        f"{pause}"
        f"{steps}\n"
        "Recent execution metadata (not tool output):\n"
        f"{_recent_event_summary(plan)}\n"
        "Treat persisted progress as a checkpoint, not as proof that the workspace is unchanged. "
        "After a resume, re-inspect current state before any new mutation and verify after changes."
    )


def resume_prompt_context(plan: AgentPlan, user_input: str = "") -> str:
    """Build a safe process-restart continuation without persisting raw tool results."""
    extra = str(user_input or "").strip()
    if extra.lower() in {
        "continue", "weiter", "fortfahren", "ok", "okay", "ja", "yes",
        "sure", "go ahead", "mach", "mach es",
    }:
        extra = ""
    suffix = f"\nAdditional user instruction for this resume: {extra[:2000]}" if extra else ""
    return (
        f"Resume persistent plan {plan.id}.\n"
        f"Original task: {plan.task[:3000]}\n"
        f"Previous pause: {plan.pause_reason or 'none recorded'}\n"
        "A bounded sanitized conversation checkpoint may have been restored, but raw tool output "
        "and incomplete provider tool protocol are intentionally not replayed. Re-inspect the current "
        "workspace before any new mutation, continue only unfinished work, and perform post-change "
        "verification before DONE."
        f"{suffix}"
    )
