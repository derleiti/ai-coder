from __future__ import annotations

import json

from aicoder import bug_reporter


def test_redaction_and_nested_secret_scrubbing():
    text = bug_reporter.redact("Authorization: Bearer abc token=secret AAAA-BBBB-CCCC-DDDD-EEEE-FFFF https://x/?key=nope")
    assert "abc" not in text and "secret" not in text and "AAAA-BBBB" not in text and "nope" not in text
    value = bug_reporter.scrub({"token": "x", "nested": {"password": "y", "safe": "ok"}})
    assert value == {"token": "[REDACTED]", "nested": {"password": "[REDACTED]", "safe": "ok"}}


def test_manual_report_queues_when_offline(monkeypatch, tmp_path):
    monkeypatch.setattr(bug_reporter, "_state_root", lambda: tmp_path)
    monkeypatch.setattr(bug_reporter, "_post", lambda payload, timeout=4.0: False)
    bug_reporter._config = {"app": "AICoder", "repo": "ai-coder", "version": "test", "channel": "test"}
    result = bug_reporter.submit_manual("token=should-hide")
    assert result == {"ok": False, "queued": True}
    rows = json.loads((tmp_path / "pending-reports.json").read_text())
    assert len(rows) == 1
    assert "should-hide" not in json.dumps(rows)


def test_startup_selftest_uses_private_state_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(bug_reporter, "_state_root", lambda: tmp_path)
    result = bug_reporter.startup_selftest()
    assert result["ok"] is True
    assert result["checks"]["state_dir_writable"] is True
