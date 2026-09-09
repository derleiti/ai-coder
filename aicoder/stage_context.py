"""Deterministic cross-stage context for staged AICoder team runs.

TaskContract is immutable user intent. StageOff is coordinator-curated working
memory. RuntimeTruth is machine-derived evidence that coordinators and later
models may summarize but must never override.
"""
from __future__ import annotations

import json
from typing import Any

from .task_contract import TaskContract


def _stage_name(stage: Any) -> str:
    return str(getattr(stage, "value", stage) or "").strip()


def build_runtime_truth(current: dict[str, Any], stage: Any, stage_payload: Any) -> dict[str, Any]:
    """Return cumulative machine-derived facts after a successful stage output."""
    name = _stage_name(stage)
    previous = dict(current.get("runtime_truth") or {})
    completed = [str(item) for item in previous.get("completed_stage_outputs") or [] if str(item)]
    if name and name not in completed:
        completed.append(name)

    truth: dict[str, Any] = {
        "schema": "aicoder-runtime-truth-v1",
        "last_completed_stage_output": name,
        "completed_stage_outputs": completed,
        "implementation_state": str(previous.get("implementation_state") or "not_runtime_verified"),
        "verified_candidate_count": int(previous.get("verified_candidate_count") or 0),
        "merge_delta": dict(previous.get("merge_delta") or {}),
        "final_verification_passed": bool(previous.get("final_verification_passed")),
        "persistent_write_state": str(previous.get("persistent_write_state") or "not_performed"),
    }

    payload = stage_payload if isinstance(stage_payload, dict) else {}
    if name == "code":
        candidates = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []
        verified = [
            item for item in candidates
            if isinstance(item, dict)
            and isinstance(item.get("evaluation"), dict)
            and bool(item["evaluation"].get("verification_passed"))
        ]
        truth["verified_candidate_count"] = len(verified)
        truth["implementation_state"] = "verified_candidate_available" if verified else "not_runtime_verified"
    elif name == "merge":
        delta = payload.get("workspace_delta") if isinstance(payload.get("workspace_delta"), dict) else {}
        truth["merge_delta"] = dict(delta)
        truth["implementation_state"] = "merged_candidate_created"
    elif name == "tests_function_ok":
        rows = payload.get("verification") if isinstance(payload.get("verification"), list) else []
        required = [row for row in rows if isinstance(row, dict) and row.get("required", True)]
        passed = bool(required) and all(bool(row.get("ok")) for row in required)
        truth["final_verification_passed"] = passed
        if passed:
            truth["implementation_state"] = "final_verification_passed"
            truth["persistent_write_state"] = "verification_gate_open"
    elif name == "atomic_disk_write":
        # This StageOff pass is intentionally produced before finalize(); never claim
        # persistence until the transactional backend actually commits.
        if bool(payload.get("verification_passed")):
            truth["persistent_write_state"] = "ready_to_finalize"

    return truth



def mark_persistent_write_completed(truth: dict[str, Any]) -> dict[str, Any]:
    """Return RuntimeTruth after the transactional backend actually finalized."""
    updated = dict(truth or {})
    updated["schema"] = "aicoder-runtime-truth-v1"
    updated["persistent_write_state"] = "persisted"
    return updated

def runtime_completion_summary(truth: dict[str, Any]) -> str:
    """Human-readable completion text derived only from RuntimeTruth."""
    completed = ", ".join(str(item) for item in truth.get("completed_stage_outputs") or []) or "none"
    lines = [f"Runtime-confirmed stage outputs: {completed}."]
    count = int(truth.get("verified_candidate_count") or 0)
    if count:
        lines.append(f"Verified coding candidates: {count}.")
    state = str(truth.get("implementation_state") or "not_runtime_verified")
    lines.append(f"Implementation evidence state: {state}.")
    if state == "not_runtime_verified":
        lines.append("No implementation item is runtime-confirmed complete.")
    lines.append(
        "Final deterministic verification: "
        + ("passed." if truth.get("final_verification_passed") else "not yet established.")
    )
    lines.append(f"Persistent write state: {truth.get('persistent_write_state') or 'not_performed'}.")
    return "\n".join(lines)


def _stageoff_dict(stage_input: Any) -> dict[str, Any]:
    for value in (getattr(stage_input, "raw", ""), getattr(stage_input, "compact", "")):
        try:
            parsed = json.loads(str(value or ""))
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def build_stage_initialization(
    *, stage_input: Any, contract: TaskContract, current_stage: str,
    sought: str, permissions: str,
) -> str:
    """Build a compact orientation header for a fresh model conversation."""
    stageoff = _stageoff_dict(stage_input)
    working = stageoff.get("working_memory") if isinstance(stageoff.get("working_memory"), dict) else {}
    truth = stageoff.get("runtime_truth") if isinstance(stageoff.get("runtime_truth"), dict) else {}

    required = "\n".join(f"- {item}" for item in contract.requirements[:12]) or "- Follow every requirement in the immutable user task."
    prohibited = "\n".join(f"- {item}" for item in contract.prohibitions[:12]) or "- No additional explicit prohibition extracted."
    acceptance = "\n".join(f"- {item}" for item in contract.acceptance_commands) or "- Use the StageOff/user task acceptance criteria."
    open_items = str(working.get("open_items") or "No coordinator-curated open-item list; derive only from TaskContract and RuntimeTruth.")[:6000]
    next_steps = str(working.get("next_stage_instructions") or "Process only the unresolved work for this stage.")[:5000]

    return (
        "=== FRESH STAGE INITIALIZATION ===\n"
        f"CURRENT STAGE: {current_stage}\n\n"
        "GIVEN / AUTHORITATIVE:\n"
        "- TaskContract = immutable user intent and hard constraints.\n"
        "- RuntimeTruth = machine-derived facts; it outranks coordinator/model completion claims.\n"
        "- StageOff = coordinator-curated working memory; use it for decisions, failures, and next work.\n\n"
        "REQUIRED:\n" + required + "\n\n"
        "SOUGHT FOR THIS STAGE:\n- " + sought.strip() + "\n\n"
        "TO PROCESS / STILL OPEN:\n" + open_items + "\n\n"
        "COORDINATOR NEXT-STAGE INSTRUCTIONS:\n" + next_steps + "\n\n"
        "PROHIBITED:\n" + prohibited + "\n\n"
        "TOOLS / PERMISSIONS:\n" + permissions.strip() + "\n\n"
        "ACCEPTANCE STILL AUTHORITATIVE:\n" + acceptance + "\n\n"
        "RUNTIME TRUTH:\n" + json.dumps(truth, ensure_ascii=False, indent=2, sort_keys=True) + "\n\n"
        "SUCCESS RULE:\n"
        "Do not claim implementation, verification, or persistence complete unless RuntimeTruth/tool evidence establishes it. "
        "Work only on this stage's unresolved job; inspect -> act -> verify, and do not repeat an unchanged failure cycle.\n"
        "=== END STAGE INITIALIZATION ==="
    )
