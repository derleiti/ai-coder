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


def test_network_filters_and_opens_conversation_in_main_chat(monkeypatch, tmp_path):
    from aicoder import shared_notify as shared
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(shared, "STATE_FILE", tmp_path / "state-network.json")
    shared.save_shared_notify_state(shared.SharedNotifyState(enabled=False, device_id="dev_network", handle="@zombie"))
    widget = SharedNotifyWidget()
    widget._show_directory({"endpoints": [
        {"handle": "@claude-zombie", "label": "Claude", "kind": "ai", "online": True, "availability": "available", "activity": "idle", "accept_human_chat": True, "accept_ai_chat": True, "capabilities": ["chat"]},
        {"handle": "@zombie", "label": "Human", "kind": "client", "online": True, "availability": "available", "activity": "idle", "accept_human_chat": True, "accept_ai_chat": False, "capabilities": ["chat"]},
    ]})
    conversation = {"conversation_id": "conv_1", "title": "Architecture", "kind": "group", "members": [{"handle": "@zombie"}, {"handle": "@claude-zombie"}]}
    widget._show_conversations({"conversations": [conversation]})
    assert widget.directory.rowCount() == 2
    assert widget.directory.item(0, 0).text().startswith("● @claude-zombie")
    widget.network_filter.setText("claude")
    assert widget.directory.rowCount() == 1
    opened = []
    widget.conversation_open_requested.connect(opened.append)
    widget.conversations.setCurrentRow(0)
    widget._open_selected_conversation()
    assert opened[0]["conversation_id"] == "conv_1"
    widget.close(); app.processEvents()


def test_chat_hub_opens_named_notify_tab(monkeypatch, tmp_path):
    from aicoder import shared_notify as shared
    from aicoder.gui.chat_hub_widget import ChatHubWidget
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(shared, "STATE_FILE", tmp_path / "state-hub.json")
    shared.save_shared_notify_state(shared.SharedNotifyState(enabled=False, device_id="dev_hub", handle="@zombie"))
    monkeypatch.setattr("aicoder.gui.notify_chat_widget.NotifyConversationWidget.refresh", lambda self: None)
    hub = ChatHubWidget(settings_ref=None)
    conversation = {"conversation_id":"conv_direct","kind":"direct","title":"","members":[{"handle":"@zombie"},{"handle":"@claude"}]}
    hub.open_conversation(conversation)
    assert hub.tabs.count() == 2
    assert hub.tabs.tabText(1) == "@claude"
    hub.open_conversation(conversation)
    assert hub.tabs.count() == 2
    hub.close(); app.processEvents()


def test_main_window_routes_notify_conversation_to_chat_hub(monkeypatch, tmp_path):
    from aicoder import shared_notify as shared
    from aicoder.gui.main_window import MainWindow
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(shared, "STATE_FILE", tmp_path / "state-main-window.json")
    shared.save_shared_notify_state(shared.SharedNotifyState(enabled=False, device_id="dev_main", handle="@zombie"))
    monkeypatch.setattr("aicoder.gui.notify_chat_widget.NotifyConversationWidget.refresh", lambda self: None)
    monkeypatch.setattr(MainWindow, "_setup_system_log_monitor", lambda self: None)
    window = MainWindow()
    conversation = {"conversation_id":"conv_arch","kind":"group","title":"Architecture","members":[{"handle":"@zombie"},{"handle":"@claude"}]}
    window.network_tab.conversation_open_requested.emit(conversation)
    assert window.tabs.currentWidget() is window.chat_tab
    assert window.chat_tab.tabs.count() == 2
    assert window.chat_tab.tabs.tabText(1) == "Architecture"
    window.close(); app.processEvents()


def test_notify_chat_renders_reply_reusing_parent_correlation(monkeypatch, tmp_path):
    from aicoder import shared_notify as shared
    from aicoder.gui.notify_chat_widget import NotifyConversationWidget
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(shared, "STATE_FILE", tmp_path / "state-notify-chat.json")
    shared.save_shared_notify_state(shared.SharedNotifyState(enabled=False, device_id="dev_chat", handle="@zombie"))
    monkeypatch.setattr(NotifyConversationWidget, "refresh", lambda self: None)
    widget = NotifyConversationWidget({
        "conversation_id": "conv_direct",
        "kind": "direct",
        "members": [{"handle": "@zombie"}, {"handle": "@ailinux-ollama-kimi-k3"}],
    })
    parent = {
        "message_id": "msg_parent", "correlation_id": "grp_same", "sender_handle": "@zombie",
        "kind": "human_chat", "body": "sag nur notify funktioniert.", "status": "acknowledged",
    }
    reply = {
        "message_id": "msg_reply", "correlation_id": "grp_same", "sender_handle": "@ailinux-ollama-kimi-k3",
        "kind": "ai_optimization", "body": "notify funktioniert.", "status": "acknowledged",
    }
    widget._show_history({"messages": [parent]})
    widget._show_history({"messages": [parent, reply]})
    assert len(widget._last_signature) == 2
    assert widget._last_signature[0] != widget._last_signature[1]
    text = widget.log.toPlainText()
    assert "sag nur notify funktioniert." in text
    assert "notify funktioniert." in text
    assert "@ailinux-ollama-kimi-k3" in text
    widget.close(); app.processEvents()
