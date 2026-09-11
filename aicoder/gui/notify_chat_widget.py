"""Conversation chat widget for Shared Notify threads."""
from __future__ import annotations

import html
from typing import Any, Callable

from PyQt6.QtCore import QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPushButton, QTextEdit, QVBoxLayout, QWidget

from .. import shared_notify as shared


class _ConversationWorker(QThread):
    success = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, operation: Callable[[], Any], parent=None):
        super().__init__(parent)
        self.operation = operation

    def run(self):
        try:
            self.success.emit(self.operation())
        except Exception as exc:
            self.error.emit(str(exc))


class NotifyConversationWidget(QWidget):
    """Render one Shared Notify conversation inside the main Chat area."""

    title_changed = pyqtSignal(str)

    def __init__(self, conversation: dict[str, Any], parent=None):
        super().__init__(parent)
        self.conversation = dict(conversation or {})
        self.conversation_id = str(self.conversation.get("conversation_id") or "")
        self._workers: set[_ConversationWorker] = set()
        self._busy = False
        self._last_signature: tuple[str, ...] = ()
        self._build()
        self.refresh()
        self._timer = QTimer(self)
        self._timer.setInterval(4000)
        self._timer.timeout.connect(self._refresh_if_idle)
        self._timer.start()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self.header = QLabel(self.display_title())
        self.header.setObjectName("Caption")
        layout.addWidget(self.header)

        self.log = QTextEdit()
        self.log.setObjectName("ChatLog")
        self.log.setReadOnly(True)
        layout.addWidget(self.log, 1)

        self.status = QLabel("Shared Notify conversation")
        self.status.setObjectName("Caption")
        layout.addWidget(self.status)

        row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("Message…  @handle mentions are supported")
        self.send_button = QPushButton("Send")
        self.send_button.setObjectName("PrimaryButton")
        row.addWidget(self.input, 1)
        row.addWidget(self.send_button)
        layout.addLayout(row)

        self.input.returnPressed.connect(self.send_message)
        self.send_button.clicked.connect(self.send_message)

    def display_title(self) -> str:
        title = str(self.conversation.get("title") or "").strip()
        members = [str(m.get("handle") or "") for m in self.conversation.get("members") or []]
        state = shared.load_shared_notify_state(create_identity=False)
        me = str(state.handle or "").lstrip("@")
        others = [h for h in members if h.lstrip("@") != me]
        if title:
            return title
        if self.conversation.get("kind") == "direct" and others:
            return others[0]
        return ", ".join(others or members) or "Conversation"

    def update_conversation(self, conversation: dict[str, Any]):
        self.conversation = dict(conversation or self.conversation)
        self.header.setText(self.display_title())
        self.title_changed.emit(self.display_title())

    def _run(self, operation: Callable[[], Any], success: Callable[[Any], None]):
        if self._busy:
            return False
        self._busy = True
        worker = _ConversationWorker(operation, self)
        self._workers.add(worker)
        worker.success.connect(success)
        worker.error.connect(self._show_error)
        worker.finished.connect(lambda w=worker: self._finished(w))
        worker.start()
        return True

    def _finished(self, worker):
        self._workers.discard(worker)
        self._busy = False
        worker.deleteLater()

    def _show_error(self, message: str):
        self.status.setText(f"Error: {message}")

    def refresh(self):
        if not self.conversation_id:
            self.status.setText("Conversation has no ID")
            return
        self._run(
            lambda: shared._client().notify_conversation_history(self.conversation_id),
            self._show_history,
        )

    def _refresh_if_idle(self):
        if self.isVisible() and not self._busy:
            self.refresh()

    def _show_history(self, result: dict[str, Any]):
        messages = list((result or {}).get("messages") or [])
        signature = tuple(str(m.get("correlation_id") or m.get("message_id") or "") for m in messages)
        if signature == self._last_signature:
            self.status.setText(f"{len(messages)} messages · synced")
            return
        self._last_signature = signature
        self.log.clear()
        state = shared.load_shared_notify_state(create_identity=False)
        me = str(state.handle or "").lstrip("@")
        for message in messages:
            sender = str(message.get("sender_handle") or "@unknown")
            body = html.escape(str(message.get("body") or "")).replace("\n", "<br>")
            kind = html.escape(str(message.get("kind") or "chat"))
            title = str(message.get("title") or "").strip()
            safe_sender = html.escape(sender)
            if me and sender.lstrip("@") == me:
                label = "You"
            else:
                label = safe_sender
            meta = f" [{kind}]"
            if title:
                meta += " · " + html.escape(title)
            if me:
                body = body.replace(f"@{html.escape(me)}", f"<b>@{html.escape(me)}</b>")
            self.log.append(f"<b>{label}</b><span style='color:#777'>{meta}</span><br>{body}<hr>")
        self.status.setText(f"{len(messages)} messages · synced")

    def send_message(self):
        body = self.input.text().strip()
        if not body or not self.conversation_id:
            return
        state = shared.load_shared_notify_state(create_identity=False)
        if not state.enabled or not state.endpoint_id:
            self.status.setText("Shared Notify is disabled")
            return
        payload = {"sender_endpoint_id": state.endpoint_id, "kind": "human_chat", "body": body}
        self.input.clear()
        self.send_button.setEnabled(False)

        def operation():
            client = shared._client()
            client.notify_conversation_send(self.conversation_id, payload)
            return client.notify_conversation_history(self.conversation_id)

        def success(result):
            self._show_history(result)
            self.send_button.setEnabled(True)

        if not self._run(operation, success):
            self.input.setText(body)
            self.send_button.setEnabled(True)

    def closeEvent(self, event):
        self._timer.stop()
        super().closeEvent(event)
