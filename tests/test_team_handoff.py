from __future__ import annotations

import unittest

from aicoder.team_handoff import CODE_PLAN_SECTIONS, RESEARCH_SECTIONS, make_handoff
from aicoder.team_orchestrator import AgentStageResult, _blind_merge_prompt, _build_planner_prompt, _contract_issues, _planning_approval


class HandoffEnvelopeTests(unittest.TestCase):
    def test_id_is_stable_and_payload_is_bounded(self):
        raw = "alpha " * 5000
        first = make_handoff("demo", raw, max_chars=1200)
        second = make_handoff("demo", raw, max_chars=1200)
        self.assertEqual(first.handoff_id, second.handoff_id)
        self.assertLessEqual(first.compact_chars, 1200)
        self.assertGreater(first.saved_chars, 0)
        self.assertIn("[HANDOFF id=ho-", first.render())

    def test_structured_research_keeps_all_evidence_sections(self):
        raw = "\n".join([
            "FINDINGS:\n" + "fact " * 500,
            "SOURCES:\nhttps://example.invalid/source\n" + "source " * 300,
            "APPLICABILITY:\n" + "apply " * 300,
            "RISKS:\n" + "risk " * 300,
            "RECOMMENDATIONS:\n" + "recommend " * 300,
        ])
        handoff = make_handoff("research", raw, max_chars=3000, section_labels=RESEARCH_SECTIONS)
        for heading in RESEARCH_SECTIONS:
            self.assertIn(heading + ":", handoff.compact)
        self.assertIn("https://example.invalid/source", handoff.compact)
        self.assertLessEqual(handoff.compact_chars, 3000)


    def test_unstructured_tool_json_is_rejected_as_stage_contract(self):
        text = '{"tool":"file_tree","path":"/tmp/project"}'
        issues = _contract_issues(text, ("SESSION MEMORY", "RESEARCH PLAN"))
        self.assertTrue(any("tool-call syntax" in issue for issue in issues))
        self.assertTrue(any("missing section" in issue for issue in issues))

    def test_markdown_headings_are_valid_stage_contract_sections(self):
        labels = (
            "SESSION MEMORY", "RESEARCH PLAN", "R1 PRIMARY SOURCES",
            "R2 BEST PRACTICES", "R3 SECURITY RELIABILITY",
            "R4 ALTERNATIVE ARCHITECTURES", "EVIDENCE GAPS",
            "NEXT STAGE INSTRUCTIONS",
        )
        text = """# SESSION MEMORY
state facts

# RESEARCH PLAN
plan

## R1 PRIMARY SOURCES — Authoritative References Needed
refs

## R2 BEST PRACTICES — Engineering Patterns
patterns

## R3 SECURITY RELIABILITY — Failure/Recovery/Observability
reliability

## R4 ALTERNATIVE ARCHITECTURES — Trade-offs
tradeoffs

# EVIDENCE GAPS (Must Be Resolved, Not Guessed)
gaps

# NEXT STAGE INSTRUCTIONS
next
"""
        self.assertEqual(_contract_issues(text, labels), [])

    def test_colon_contract_sections_still_work_with_inline_body(self):
        text = "SESSION MEMORY: state facts\nRESEARCH PLAN: plan details\n"
        self.assertEqual(_contract_issues(text, ("SESSION MEMORY", "RESEARCH PLAN")), [])

    def test_markdown_parent_heading_may_be_populated_by_required_child_sections(self):
        text = """# SESSION MEMORY
state

# RESEARCH PLAN

## R1 PRIMARY SOURCES: Sources
source facts

## R2 BEST PRACTICES
practice facts
"""
        labels = ("SESSION MEMORY", "RESEARCH PLAN", "R1 PRIMARY SOURCES", "R2 BEST PRACTICES")
        self.assertEqual(_contract_issues(text, labels), [])

    def test_empty_peer_heading_is_still_invalid(self):
        text = """# SESSION MEMORY
state

# RESEARCH PLAN

# R1 PRIMARY SOURCES
source facts
"""
        issues = _contract_issues(text, ("SESSION MEMORY", "RESEARCH PLAN", "R1 PRIMARY SOURCES"))
        self.assertIn("missing section: RESEARCH PLAN", issues)

    def test_bold_markdown_contract_labels_are_accepted(self):
        text = """SESSION MEMORY: compact state

RESEARCH PLAN: concise plan

**R1 PRIMARY SOURCES: authoritative docs**
- source

**R2 BEST PRACTICES: patterns**
- pattern

**R3 SECURITY RELIABILITY: risks**
- risk

**R4 ALTERNATIVE ARCHITECTURES: tradeoffs**
- tradeoff

EVIDENCE GAPS: none critical

NEXT STAGE INSTRUCTIONS: research
"""
        labels = (
            "SESSION MEMORY", "RESEARCH PLAN", "R1 PRIMARY SOURCES", "R2 BEST PRACTICES",
            "R3 SECURITY RELIABILITY", "R4 ALTERNATIVE ARCHITECTURES", "EVIDENCE GAPS",
            "NEXT STAGE INSTRUCTIONS",
        )
        self.assertEqual(_contract_issues(text, labels), [])

    def test_bounded_handoff_preserves_newest_tail(self):
        raw = "START-MARKER\n" + ("middle-line\n" * 3000) + "LATEST-STAGE-MARKER\nNEXT STAGE INSTRUCTIONS: keep this"
        handoff = make_handoff("stageoff", raw, max_chars=1800)
        self.assertIn("START-MARKER", handoff.compact)
        self.assertIn("LATEST-STAGE-MARKER", handoff.compact)
        self.assertIn("NEXT STAGE INSTRUCTIONS", handoff.compact)
        self.assertIn("handoff compacted", handoff.compact)

    def test_planning_policy_allows_verification_but_not_file_mutation(self):
        self.assertTrue(_planning_approval("test", {"command": "python -m pytest"}))
        self.assertTrue(_planning_approval("lint", {"command": "python -m compileall ."}))
        self.assertFalse(_planning_approval("file_edit", {"path": "app.py", "content": "x"}))

    def test_code_contract_projection_preserves_acceptance_and_verification(self):
        raw = (
            "OBJECTIVE:\nfix it\nREQUIREMENTS:\n" + "r " * 4000
            + "\nACCEPTANCE TESTS:\nacceptance-marker\n"
            + "VERIFICATION:\nverification-marker\nMERGE CRITERIA:\nmerge-marker\nRISKS:\nrisk-marker\n"
        )
        handoff = make_handoff("code", raw, max_chars=3500, section_labels=CODE_PLAN_SECTIONS)
        self.assertIn("ACCEPTANCE TESTS:", handoff.compact)
        self.assertIn("acceptance-marker", handoff.compact)
        self.assertIn("VERIFICATION:", handoff.compact)
        self.assertIn("verification-marker", handoff.compact)


class TeamPromptCompactionTests(unittest.TestCase):
    def test_planner_gets_compact_research_handoff_not_full_transcript(self):
        marker = "SHOULD-NOT-REACH-PLANNER"
        report = AgentStageResult(
            role="research:primary_sources", model="provider/model", status="completed",
            response="FINDINGS:\n" + ("fact " * 3000) + "\nSOURCES:\nsource\nRISKS:\n" + marker,
            elapsed_ms=1, evidence={"externally_verified": True, "successful_tools": ["search"]},
        )
        prompt = _build_planner_prompt("task", "repo", [report])
        self.assertIn("HANDOFF id=ho-", prompt)
        self.assertLess(len(prompt), 6000)
        # The structured projection retains risk headings but bounded content does not copy an arbitrary huge transcript.
        self.assertNotIn("fact " * 1000, prompt)
        self.assertNotIn("provider/model", prompt)

    def test_blind_merge_prompt_bounds_large_candidate_diffs(self):
        huge_diff = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n" + ("+changed line\n" * 20000)
        evidence = [
            {
                "candidate_id": f"cand-{i}", "score": 100 - i, "verification_passed": True,
                "checks": {"tests": {"ok": True, "exit_code": 0, "elapsed_ms": 5, "required": True, "output": "x" * 10000}},
                "delta": {"changed_count": 1, "deleted_count": 0, "changed": ["x.py"]},
                "diff": huge_diff, "snapshot": f".aicoder-team/candidates/cand-{i}",
            }
            for i in range(4)
        ]
        prompt = _blind_merge_prompt("task", "OBJECTIVE:\nfix\nREQUIREMENTS:\nsafe", evidence)
        self.assertLess(len(prompt), 40000)
        self.assertIn("cand-0", prompt)
        self.assertIn("x.py", prompt)
        self.assertNotIn("x" * 1000, prompt)
        self.assertNotIn("model", prompt.lower())
        self.assertNotIn("provider", prompt.lower())


if __name__ == "__main__":
    unittest.main()
