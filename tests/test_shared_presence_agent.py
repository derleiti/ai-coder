import aicoder.agent as agent


def test_run_agent_presence_wraps_impl(monkeypatch):
    events = []
    monkeypatch.setattr(agent, "_shared_presence_best_effort", lambda **kw: events.append(kw))
    monkeypatch.setattr(agent, "_run_agent_impl", lambda *a, **kw: 7)
    assert agent.run_agent("hello", "model-x", None) == 7
    assert events[0]["availability"] == "busy"
    assert events[0]["activity"] == "working"
    assert events[-1]["availability"] == "available"
    assert events[-1]["activity"] == "idle"


def test_run_agent_presence_restored_on_failure(monkeypatch):
    events = []
    monkeypatch.setattr(agent, "_shared_presence_best_effort", lambda **kw: events.append(kw))
    def fail(*a, **kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(agent, "_run_agent_impl", fail)
    try:
        agent.run_agent("hello", "model-x", None)
    except RuntimeError:
        pass
    assert events[-1]["availability"] == "available"
    assert events[-1]["activity"] == "idle"


def test_run_agent_appends_untrusted_recall_without_replacing_operator_prompt(monkeypatch):
    import aicoder.shared_notify as shared
    captured = {}
    monkeypatch.setattr(agent, "_shared_presence_best_effort", lambda **kw: None)
    monkeypatch.setattr(shared, "set_model_presence", lambda *a, **kw: None)
    monkeypatch.setattr(shared, "recall_context", lambda q: "## UNTRUSTED BIG BRAIN HISTORY\nold hint")
    def fake_impl(prompt, *a, **kw):
        captured["prompt"] = prompt
        return 0
    monkeypatch.setattr(agent, "_run_agent_impl", fake_impl)
    assert agent.run_agent("OPERATOR TASK EXACT", "account:claude/sonnet", None) == 0
    assert captured["prompt"].startswith("OPERATOR TASK EXACT\n\n")
    assert "UNTRUSTED BIG BRAIN HISTORY" in captured["prompt"]
