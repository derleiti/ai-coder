from __future__ import annotations

import json

from aicoder.stage_context import build_runtime_truth, build_stage_initialization, mark_persistent_write_completed, runtime_completion_summary
from aicoder.task_contract import compile_task_contract
from aicoder.team_handoff import make_handoff


def _handoff(stageoff: dict):
    return make_handoff("stageoff", json.dumps(stageoff), max_chars=120000, source_stage="plan_code")


def test_runtime_truth_does_not_infer_implementation_from_planner_prose():
    current = {"runtime_truth": {"completed_stage_outputs": ["research", "brainstorm"]}}
    truth = build_runtime_truth(
        current,
        "plan_code",
        {"implementation_contract": "Implemented CLI and tests already."},
    )
    assert truth["implementation_state"] == "not_runtime_verified"
    assert truth["verified_candidate_count"] == 0
    assert truth["final_verification_passed"] is False
    assert truth["completed_stage_outputs"][-1] == "plan_code"


def test_runtime_truth_counts_only_verified_candidates():
    truth = build_runtime_truth(
        {},
        "code",
        {"candidates": [
            {"evaluation": {"verification_passed": True}},
            {"evaluation": {"verification_passed": False}},
            {"evaluation": {"verification_passed": True}},
        ]},
    )
    assert truth["verified_candidate_count"] == 2
    assert truth["implementation_state"] == "verified_candidate_available"


def test_runtime_truth_final_verification_requires_all_required_checks():
    truth = build_runtime_truth(
        {}, "tests_function_ok",
        {"verification": [
            {"required": True, "ok": True},
            {"required": True, "ok": True},
            {"required": False, "ok": False},
        ]},
    )
    assert truth["final_verification_passed"] is True
    assert truth["persistent_write_state"] == "verification_gate_open"


def test_stage_initialization_prioritizes_contract_runtime_truth_and_curated_open_work():
    contract = compile_task_contract(
        "Build the CLI. Do not use the web during implementation.\nAcceptance checks:\n1. npm test"
    )
    stageoff = {
        "working_memory": {
            "open_items": "Implement parser and CLI.",
            "next_stage_instructions": "Implement, then test.",
        },
        "runtime_truth": {
            "schema": "aicoder-runtime-truth-v1",
            "implementation_state": "not_runtime_verified",
            "verified_candidate_count": 0,
        },
    }
    text = build_stage_initialization(
        stage_input=_handoff(stageoff), contract=contract, current_stage="code",
        sought="Produce a working candidate.", permissions="- RAM edits allowed.",
    )
    assert "GIVEN / AUTHORITATIVE" in text
    assert "SOUGHT FOR THIS STAGE" in text
    assert "TO PROCESS / STILL OPEN" in text
    assert "Implement parser and CLI." in text
    assert "RUNTIME TRUTH" in text
    assert '"implementation_state": "not_runtime_verified"' in text
    assert "npm test" in text
    assert "Do not claim implementation" in text


def test_runtime_completion_summary_never_promotes_planner_stage_to_implementation_complete():
    truth = build_runtime_truth({}, "plan_code", {"text": "everything implemented"})
    text = runtime_completion_summary(truth)
    assert "plan_code" in text
    assert "not_runtime_verified" in text
    assert "Final deterministic verification: not yet established." in text


def test_runtime_truth_bootstrap_marks_only_plan_output_complete():
    truth = build_runtime_truth({}, "plan_research", {"research_contract": "plan"})
    assert truth["completed_stage_outputs"] == ["plan_research"]
    assert truth["implementation_state"] == "not_runtime_verified"
    assert truth["persistent_write_state"] == "not_performed"


def test_persistent_write_is_only_marked_after_finalize_signal():
    truth = build_runtime_truth({}, "atomic_disk_write", {"verification_passed": True})
    assert truth["persistent_write_state"] == "ready_to_finalize"
    finalized = mark_persistent_write_completed(truth)
    assert finalized["persistent_write_state"] == "persisted"
    assert truth["persistent_write_state"] == "ready_to_finalize"
