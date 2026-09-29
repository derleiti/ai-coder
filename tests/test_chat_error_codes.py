from aicoder.gui.chat_widget import _runtime_error_code


def test_provider_auth_errors_are_not_generic_runtime_errors():
    assert _runtime_error_code("Claude OAuth session is expired or revoked; reconnect the Claude account") == "E_PROVIDER_AUTH"


def test_provider_quota_errors_are_distinct():
    assert _runtime_error_code("Claude account usage/rate limit reached; retry after the provider limit resets") == "E_PROVIDER_QUOTA"
