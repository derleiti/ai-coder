from __future__ import annotations

import unittest

from aicoder.team_handoff import CODE_PLAN_SECTIONS, RESEARCH_SECTIONS, make_handoff
from aicoder.team_orchestrator import AgentStageResult, _blind_merge_prompt, _build_planner_prompt


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
