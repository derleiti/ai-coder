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
