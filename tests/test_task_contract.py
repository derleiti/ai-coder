from aicoder.task_contract import compile_task_contract


def test_contract_compiles_hard_prohibitions_and_acceptance_commands():
    contract = compile_task_contract("""Build locally. Do not browse the web. Never inspect the TriForce backend.
Acceptance checks:
1. python -m pytest -q
2. python -m demo --demo
""")
    assert contract.forbid_web is True
    assert contract.forbid_triforce_backend is True
    assert contract.external_research_required is False
    assert contract.acceptance_commands == ("python -m pytest -q", "python -m demo --demo")
    assert contract.tool_denial("search", {"mode": "all"})
    assert contract.tool_denial("web_fetch_local", {"url": "https://example.com"})
    assert contract.tool_denial("mcp.triforce_remote.status", {})
    assert contract.tool_denial("file_read", {"path": "README.md"}) is None


def test_external_research_signal_is_suppressed_by_explicit_no_web():
    contract = compile_task_contract(
        "Check provider compatibility, but do not browse the internet; use only local repository evidence."
    )
    assert contract.external_research_required is False


def test_contract_prompt_projection_is_machine_truth_summary():
    contract = compile_task_contract("Must add tests. Do not browse the web.")
    projection = contract.prompt_projection()
    assert "AUTHORITATIVE TASK CONTRACT" in projection
    assert "Web/network use outside research forbidden: yes" in projection
    assert "Must add tests" in projection


def test_global_no_web_does_not_disable_research_stage_web_by_default():
    contract = compile_task_contract("Do not browse the web during implementation.")
    assert contract.forbid_web is True
    assert contract.forbid_research_web is False


def test_explicit_research_no_web_is_separate_hard_constraint():
    contract = compile_task_contract("Researchers must not browse the web; use local evidence only.")
    assert contract.forbid_web is True
    assert contract.forbid_research_web is True


def test_implementation_only_no_web_keeps_external_research_signal():
    contract = compile_task_contract(
        "Check the latest API docs. Do not browse the web during implementation. Researchers may research the topic."
    )
    assert contract.forbid_web is True
    assert contract.forbid_research_web is False
    assert contract.external_research_required is True


def test_acceptance_check_supports_expected_nonzero_by_ordinal():
    contract = compile_task_contract("""Acceptance checks:\n1. python -m demo --ok\n2. python -m demo --fail-on-anomaly\n#2 EXPECTED NONZERO; this is success.\n""")
    assert contract.acceptance_checks[0].expected_exit_codes == (0,)
    assert contract.acceptance_checks[0].expected_nonzero is False
    assert contract.acceptance_checks[1].expected_exit_codes == ()
    assert contract.acceptance_checks[1].expected_nonzero is True


def test_acceptance_check_supports_exact_expected_exit_code():
    contract = compile_task_contract("""Acceptance checks:\n1. python -m demo --probe\nCheck 1 expected exit code 7.\n""")
    assert contract.acceptance_checks[0].expected_exit_codes == (7,)
    assert contract.acceptance_checks[0].expected_nonzero is False


def test_requirements_section_preserves_imperative_bullets_without_marker_words():
    contract = compile_task_contract("""Build a CLI.
Requirements:
- Python standard library only; no external packages.
- Summarize candidate quorum and merge audit events.
- Human-readable output by default and deterministic --json output.
- Do not browse the web during implementation.
Acceptance checks:
1. python -m demo --help
""")
    assert "Summarize candidate quorum and merge audit events." in contract.requirements
    assert "Human-readable output by default and deterministic --json output." in contract.requirements
    assert any("no external packages" in item.lower() for item in contract.prohibitions)
    assert any("do not browse the web" in item.lower() for item in contract.prohibitions)
    assert contract.acceptance_commands == ("python -m demo --help",)


def test_runtime_guard_style_requirements_are_not_dropped_from_contract():
    contract = compile_task_contract("""Build runtime guard.
Requirements:
- Parse one or more JSONL files and stdin; malformed lines must be tolerated and counted.
- Summarize run IDs, pipeline stages, RuntimeTruth snapshots, candidate statuses, verified candidate counts, quorum events, merge contribution audit events, verification checks including expected exit semantics, terminal state, retries, stalls, and tool errors.
- Detect contradictions where model prose claims implementation that RuntimeTruth does not establish.
- Keep parsing/analysis/reporting/I-O separated into modules.
- Include realistic fixtures and unittest coverage for normal completed runs, malformed lines, contradictions, quorum, semantic stalls, merge contribution audit, deterministic JSON, stdin, and exit codes.
Acceptance checks:
1. python -m unittest discover -s tests -v
""")
    assert len(contract.requirements) >= 5
    assert any("candidate statuses" in item for item in contract.requirements)
    assert any("realistic fixtures" in item for item in contract.requirements)
    assert any("separated into modules" in item for item in contract.requirements)


def test_requirement_bullet_with_negative_word_remains_a_requirement():
    contract = compile_task_contract("""Requirements:
- Malformed lines must never crash the CLI; count and report them.
- Detect persistence before verification and completed output without final verification.
- Python standard library only; no external packages.
""")
    assert any("Malformed lines must never crash" in item for item in contract.requirements)
    assert any("without final verification" in item for item in contract.requirements)
    assert any("no external packages" in item for item in contract.requirements)
    assert any("no external packages" in item for item in contract.prohibitions)


def test_incidental_provider_event_vocabulary_does_not_force_external_research():
    contract = compile_task_contract(
        "Build a local log parser. Requirements:\n- Report provider/retry events and API error fields from local JSONL fixtures.\n"
        "- Do not browse the web during implementation.\n"
    )
    assert contract.external_research_required is False
    assert contract.forbid_web is True
    assert contract.forbid_research_web is False


def test_latest_official_api_docs_still_require_external_research():
    contract = compile_task_contract("Check the latest official API documentation for compatibility changes.")
    assert contract.external_research_required is True
