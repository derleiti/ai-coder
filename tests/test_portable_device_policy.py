from aicoder.executor import _portable_action_mutating


def test_portable_read_actions_do_not_require_mutation_approval():
    assert _portable_action_mutating("device_info", {}) is False
    assert _portable_action_mutating("process_ops", {"action": "list"}) is False
    assert _portable_action_mutating("process_ops", {"action": "get"}) is False
    assert _portable_action_mutating("service_ops", {"action": "list"}) is False
    assert _portable_action_mutating("app_ops", {"action": "list"}) is False
    assert _portable_action_mutating("window_ops", {"action": "list"}) is False


def test_portable_control_actions_require_mutation_approval():
    assert _portable_action_mutating("process_ops", {"action": "signal"}) is True
    assert _portable_action_mutating("service_ops", {"action": "restart"}) is True
    assert _portable_action_mutating("app_ops", {"action": "launch"}) is True
    assert _portable_action_mutating("window_ops", {"action": "focus"}) is True
    assert _portable_action_mutating("computer_input", {"action": "click"}) is True


def test_non_portable_tool_has_no_override():
    assert _portable_action_mutating("search", {}) is None
