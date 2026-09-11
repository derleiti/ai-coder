import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication
from aicoder.gui.shared_notify_widget import SharedNotifyWidget


def test_big_brain_status_renders_metrics(monkeypatch, tmp_path):
    from aicoder import shared_notify as shared
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(shared, "STATE_FILE", tmp_path / "state.json")
    shared.save_shared_notify_state(shared.SharedNotifyState(enabled=False, device_id="dev_test"))
    widget = SharedNotifyWidget()
    widget._show_big_brain({"episodic_memory": {
        "provider": "claude-mem", "healthy": True,
        "metrics": {"recalls": 4, "hits": 3, "injected": 2, "latency_ms": 17.25},
    }})
    text = widget.brain_status.text()
    assert "Connected" in text
    assert "claude-mem" in text
    assert "recalls 4" in text
    assert "hits 3" in text
    assert "injected 2" in text
    assert "17.2 ms" in text
    widget.close()
    app.processEvents()


def test_messenger_renders_filters_and_history(monkeypatch, tmp_path):
    from aicoder import shared_notify as shared
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(shared, "STATE_FILE", tmp_path / "state-messenger.json")
    shared.save_shared_notify_state(shared.SharedNotifyState(enabled=False, device_id="dev_messenger"))
    widget = SharedNotifyWidget()
    widget._show_directory({"endpoints": [
        {"handle": "@claude-zombie", "label": "Claude", "kind": "ai", "online": True, "availability": "available", "activity": "idle", "accept_human_chat": True, "accept_ai_chat": True, "capabilities": ["chat"]},
        {"handle": "@markus", "label": "Human", "kind": "client", "online": True, "availability": "available", "activity": "idle", "accept_human_chat": True, "accept_ai_chat": False, "capabilities": ["chat"]},
    ]})
    widget._show_conversations({"conversations": [{
        "conversation_id": "conv_1", "title": "Architecture", "members": [
            {"handle": "@markus"}, {"handle": "@claude-zombie"},
        ],
    }]})
    assert widget.directory.rowCount() == 2
    assert widget.conversations.count() == 1
    widget.network_filter.setText("claude")
    assert widget.directory.rowCount() == 1
    assert widget.conversations.count() == 1
    widget._active_conversation_id = "conv_1"
    widget._show_conversation_history({"messages": [{
        "sender_handle": "@markus", "kind": "human_chat", "body": "Hallo Team", "delivery_count": 2,
    }]})
    assert "Architecture" in widget.conversation_title.text()
    assert "Hallo Team" in widget.conversation_log.toPlainText()
    assert "2 deliveries" in widget.conversation_log.toPlainText()
    widget.close()
    app.processEvents()


def test_messenger_unread_badge_and_online_marker(monkeypatch, tmp_path):
    from aicoder import shared_notify as shared
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(shared, "STATE_FILE", tmp_path / "state-unread.json")
    shared.save_shared_notify_state(shared.SharedNotifyState(enabled=False, device_id="dev_u", handle="@markus"))
    widget = SharedNotifyWidget()
    widget._show_directory({"endpoints": [{"handle":"@claude","kind":"ai","online":True,"availability":"available","activity":"idle","capabilities":[]}]})
    assert widget.directory.item(0, 0).text().startswith("● @claude")
    widget._show_conversations({"conversations":[{"conversation_id":"conv_u","title":"Team","members":[{"handle":"@markus"},{"handle":"@claude"}]}]})
    monkeypatch.setattr(shared, "drain_received_messages", lambda: [{"message_id":"m1","metadata":{"conversation_id":"conv_u"}}])
    widget._refresh_if_visible()
    assert "[1]" in widget.conversations.item(0).text()
    widget.close(); app.processEvents()
