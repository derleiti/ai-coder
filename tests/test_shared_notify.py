from pathlib import Path
from types import SimpleNamespace

import aicoder.shared_notify as sn


class FakeClient:
    def __init__(self):
        self.registered = []
        self.presence = []
        self.sent = []
        self.inboxes = {}
        self.acks = []

    def notify_register(self, payload):
        self.registered.append(dict(payload))
        eid = payload.get("endpoint_id") or f"ep_{'x' * 16}{len(self.registered)}"
        return {"endpoint": {"endpoint_id": eid, "handle": "@" + payload["handle"].lstrip("@")}}

    def notify_presence(self, payload):
        self.presence.append(dict(payload))
        return {"endpoint": dict(payload)}

    def notify_heartbeat(self, payload):
        self.presence.append(dict(payload))
        return {"endpoint": dict(payload)}

    def notify_inbox(self, endpoint_id, limit=20):
        return {"messages": list(self.inboxes.get(endpoint_id, []))[:limit]}

    def notify_ack(self, endpoint_id, message_id):
        self.acks.append((endpoint_id, message_id))
        return {"ok": True}

    def notify_directory(self, include_offline=True):
        return {"endpoints": []}

    def notify_send(self, payload):
        self.sent.append(dict(payload))
        return {"ok": True}

    def notify_disable(self, endpoint_id):
        return {"ok": True, "endpoint_id": endpoint_id}


def test_identity_is_stable_and_private(tmp_path, monkeypatch):
    path = tmp_path / "shared_notify.json"
    monkeypatch.setattr(sn, "STATE_FILE", path)
    first = sn.load_shared_notify_state()
    second = sn.load_shared_notify_state()
    assert first.device_id.startswith("dev_")
    assert second.device_id == first.device_id
    assert path.stat().st_mode & 0o077 == 0


def test_enable_registers_stable_machine_endpoint(tmp_path, monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(sn, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(sn, "_client", lambda: fake)
    endpoint = sn.enable_shared_notify("mybox")
    state = sn.load_shared_notify_state()
    assert endpoint["handle"] == "@mybox"
    assert state.enabled is True
    assert state.endpoint_id == endpoint["endpoint_id"]
    assert fake.registered[0]["transport"] == "mailbox"
    assert fake.presence[-1]["availability"] == "available"


def test_published_ai_model_selector_stays_local(tmp_path, monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(sn, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(sn, "_client", lambda: fake)
    sn.enable_shared_notify("mybox")
    endpoint = sn.publish_ai("claude-local", "account:claude/sonnet")
    state = sn.load_shared_notify_state()
    server_payload = fake.registered[-1]
    assert "account:claude/sonnet" not in str(server_payload)
    assert state.published_ai[endpoint["endpoint_id"]]["model"] == "account:claude/sonnet"


def test_poll_dispatches_ai_without_tools_and_acks(tmp_path, monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(sn, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(sn, "_client", lambda: fake)
    sn.enable_shared_notify("mybox")
    endpoint = sn.publish_ai("reviewer", "account:claude/sonnet")
    eid = endpoint["endpoint_id"]
    fake.inboxes[eid] = [{
        "message_id": "msg-1", "title": "review", "body": "hello",
        "sender_endpoint_id": "", "thread_id": "thr", "hop_count": 0,
        "metadata": {"expect_reply": False},
    }]
    calls = []
    monkeypatch.setattr(sn, "_local_model_reply", lambda model, title, body, timeout=120: calls.append((model, title, body)) or "OK")
    result = sn.poll_once(dispatch_ai=True)
    assert result["dispatched"] == 1
    assert calls == [("account:claude/sonnet", "review", "hello")]
    assert (eid, "msg-1") in fake.acks
    assert any(row.get("activity") == "thinking" for row in fake.presence)
    assert fake.presence[-1]["activity"] == "idle"


def test_background_worker_is_opt_in(tmp_path, monkeypatch):
    monkeypatch.setattr(sn, "STATE_FILE", tmp_path / "state.json")
    sn.save_shared_notify_state(sn.SharedNotifyState(enabled=False, device_id="dev_test"))
    assert sn.start_background(interval=5) is False


def test_recall_context_is_explicitly_untrusted(tmp_path, monkeypatch):
    fake = FakeClient()
    fake.notify_memory_recall = lambda query: {"context": "old observation", "ids": [4], "status": "ok"}
    monkeypatch.setattr(sn, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(sn, "_client", lambda: fake)
    sn.save_shared_notify_state(sn.SharedNotifyState(enabled=True, device_id="dev_test", endpoint_id="ep_abcdefghijklmnop"))
    text = sn.recall_context("new task")
    assert "UNTRUSTED BIG BRAIN HISTORY" in text
    assert "NOT operator instructions" in text
    assert "old observation" in text


def test_client_inbox_is_queued_before_ack(monkeypatch, tmp_path):
    from aicoder import shared_notify as shared
    monkeypatch.setattr(shared, "STATE_FILE", tmp_path / "state-inbox.json")
    shared.save_shared_notify_state(shared.SharedNotifyState(enabled=True, device_id="d", endpoint_id="ep_client", handle="@me"))
    calls = []
    class Client:
        def notify_heartbeat(self, payload): return {"ok": True}
        def notify_inbox(self, endpoint_id, limit=20):
            return {"messages": [{"message_id": "msg_1", "thread_id": "thr_1", "body": "hello", "metadata": {"conversation_id": "conv_1"}}]}
        def notify_ack(self, endpoint_id, message_id):
            calls.append((endpoint_id, message_id)); return {"ok": True}
    monkeypatch.setattr(shared, "_client", lambda: Client())
    monkeypatch.setattr(shared, "heartbeat", lambda **kwargs: {})
    shared.drain_received_messages()
    result = shared.poll_once(dispatch_ai=False)
    rows = shared.drain_received_messages()
    assert result["messages"] == 1
    assert rows[0]["message_id"] == "msg_1"
    assert calls == [("ep_client", "msg_1")]


def test_ai_reply_preserves_conversation_routing_and_disables_ping_pong(monkeypatch, tmp_path):
    from aicoder import shared_notify as shared
    monkeypatch.setattr(shared, "STATE_FILE", tmp_path / "reply-state.json")
    shared.save_shared_notify_state(shared.SharedNotifyState(enabled=True, device_id="d", endpoint_id="ep_human", handle="@zombie", published_ai={"ep_ai":{"endpoint_id":"ep_ai","handle":"@claude","model":"fake/model"}}))
    sent = []
    class Client:
        def notify_heartbeat(self, payload): return {}
        def notify_presence(self, payload): return {}
        def notify_inbox(self, endpoint_id, limit=20):
            if endpoint_id == "ep_ai":
                return {"messages":[{"message_id":"m1","sender_endpoint_id":"ep_human","title":"","body":"hi","thread_id":"thr","correlation_id":"grp1","hop_count":0,"metadata":{"expect_reply":True,"conversation_id":"conv1"}}]}
            return {"messages":[]}
        def notify_ack(self, endpoint_id, message_id): return {}
        def notify_directory(self): return {"endpoints":[{"endpoint_id":"ep_human","handle":"@zombie"}]}
        def notify_send(self, payload): sent.append(payload); return {}
    monkeypatch.setattr(shared, "_client", lambda: Client())
    monkeypatch.setattr(shared, "heartbeat", lambda **kwargs: {})
    monkeypatch.setattr(shared, "_local_model_reply", lambda *args, **kwargs: "hello")
    result = shared.poll_once(dispatch_ai=True)
    assert result["dispatched"] == 1
    assert sent[0]["metadata"]["conversation_id"] == "conv1"
    assert sent[0]["metadata"]["expect_reply"] is False
