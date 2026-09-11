"""Shared Notify / Presence UI for the account-scoped AILinux AI network."""
from __future__ import annotations

from typing import Any, Callable

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QInputDialog, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QSplitter, QTableWidget,
    QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)

from .. import shared_notify as shared
from ..account_providers import linked_account_catalog


class _NetworkWorker(QThread):
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


class SharedNotifyWidget(QWidget):
    """Manage stable handles, human presence and locally published AI endpoints."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._workers: set[_NetworkWorker] = set()
        self._busy = False
        self._directory_rows: list[dict[str, Any]] = []
        self._conversation_rows: list[dict[str, Any]] = []
        self._active_conversation_id = ""
        self._unread: dict[str, int] = {}
        self._last_history_signature: dict[str, tuple[str, ...]] = {}
        self._build()
        self._load_local_state()
        self._timer = QTimer(self)
        self._timer.setInterval(5000)
        self._timer.timeout.connect(self._refresh_if_visible)
        self._timer.start()

    def _build(self):
        root = QVBoxLayout(self)

        identity_box = QGroupBox("Shared Notify Identity")
        form = QFormLayout(identity_box)
        self.enabled_label = QLabel("Disabled")
        self.handle = QLineEdit()
        self.handle.setPlaceholderText("aicoder-my-machine")
        self.device_label = QLabel("-")
        self.endpoint_label = QLabel("-")
        buttons = QHBoxLayout()
        self.enable_button = QPushButton("Enable Shared Notify")
        self.rename_button = QPushButton("Claim / Rename @handle")
        self.disable_button = QPushButton("Disable")
        buttons.addWidget(self.enable_button)
        buttons.addWidget(self.rename_button)
        buttons.addWidget(self.disable_button)
        form.addRow("State", self.enabled_label)
        form.addRow("@handle", self.handle)
        form.addRow("Device", self.device_label)
        form.addRow("Endpoint", self.endpoint_label)
        form.addRow("", buttons)
        root.addWidget(identity_box)

        presence_box = QGroupBox("Human / Client Presence")
        pform = QFormLayout(presence_box)
        self.availability = QComboBox()
        self.availability.addItems([
            "available", "busy", "waiting", "blocked", "do_not_disturb", "quota_limited", "offline",
        ])
        self.activity = QComboBox()
        self.activity.addItems([
            "idle", "open_for_human_chat", "open_for_ai_chat", "working", "working_hard",
            "researching", "coding", "reviewing", "thinking", "waiting_for_operator", "waiting_for_agent",
        ])
        self.status_text = QLineEdit()
        self.status_text.setPlaceholderText("e.g. Open for AI chat about AILinux")
        accepts = QHBoxLayout()
        self.accept_human = QCheckBox("Human chat")
        self.accept_ai = QCheckBox("AI chat")
        self.accept_tasks = QCheckBox("Task proposals")
        accepts.addWidget(self.accept_human)
        accepts.addWidget(self.accept_ai)
        accepts.addWidget(self.accept_tasks)
        self.save_presence_button = QPushButton("Update Presence")
        pform.addRow("Availability", self.availability)
        pform.addRow("Activity", self.activity)
        pform.addRow("Status", self.status_text)
        pform.addRow("Open for", accepts)
        pform.addRow("", self.save_presence_button)
        root.addWidget(presence_box)

        ai_box = QGroupBox("Publish Local AI Endpoint")
        aiform = QFormLayout(ai_box)
        self.ai_handle = QLineEdit()
        self.ai_handle.setPlaceholderText("claude-zombie-pc")
        self.ai_model = QComboBox()
        self.ai_model.setEditable(True)
        self.ai_model.setPlaceholderText("account:claude/sonnet")
        self.publish_button = QPushButton("Publish AI")
        aiform.addRow("@handle", self.ai_handle)
        aiform.addRow("Local model", self.ai_model)
        aiform.addRow("", self.publish_button)
        root.addWidget(ai_box)

        brain_box = QGroupBox("Big Brain · Claude-Mem")
        brain = QHBoxLayout(brain_box)
        self.brain_status = QLabel("Not loaded")
        self.brain_status.setWordWrap(True)
        self.brain_refresh_button = QPushButton("Refresh Big Brain")
        brain.addWidget(self.brain_status, 1)
        brain.addWidget(self.brain_refresh_button)
        root.addWidget(brain_box)

        directory_box = QGroupBox("AILinux AI Network")
        dlayout = QVBoxLayout(directory_box)
        top = QHBoxLayout()
        self.directory_status = QLabel("Not loaded")
        self.refresh_button = QPushButton("Refresh Directory")
        top.addWidget(self.directory_status)
        top.addStretch()
        top.addWidget(self.refresh_button)
        dlayout.addLayout(top)
        self.directory = QTableWidget(0, 7)
        self.directory.setHorizontalHeaderLabels([
            "Handle", "Kind", "Availability", "Activity", "Human chat", "AI chat", "Capabilities",
        ])
        self.directory.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.directory.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.directory.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.directory.horizontalHeader().setStretchLastSection(True)
        dlayout.addWidget(self.directory)
        root.addWidget(directory_box, 1)

        messenger_box = QGroupBox("Messenger · Shared Notify")
        messenger = QVBoxLayout(messenger_box)
        tools = QHBoxLayout()
        self.network_filter = QLineEdit()
        self.network_filter.setPlaceholderText("Filter endpoints or conversations…")
        self.open_chat_button = QPushButton("Open Chat")
        self.create_group_button = QPushButton("Create Group")
        tools.addWidget(self.network_filter, 1)
        tools.addWidget(self.open_chat_button)
        tools.addWidget(self.create_group_button)
        messenger.addLayout(tools)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.conversations = QListWidget()
        self.conversations.setMinimumWidth(220)
        split.addWidget(self.conversations)
        chat_side = QWidget()
        chat_layout = QVBoxLayout(chat_side)
        chat_layout.setContentsMargins(0, 0, 0, 0)
        self.conversation_title = QLabel("No conversation selected")
        self.conversation_log = QTextEdit()
        self.conversation_log.setReadOnly(True)
        send_row = QHBoxLayout()
        self.conversation_input = QLineEdit()
        self.conversation_input.setPlaceholderText("Message to this conversation…")
        self.conversation_send_button = QPushButton("Send")
        send_row.addWidget(self.conversation_input, 1)
        send_row.addWidget(self.conversation_send_button)
        chat_layout.addWidget(self.conversation_title)
        chat_layout.addWidget(self.conversation_log, 1)
        chat_layout.addLayout(send_row)
        split.addWidget(chat_side)
        split.setStretchFactor(1, 1)
        messenger.addWidget(split, 1)
        root.addWidget(messenger_box, 2)

        self.enable_button.clicked.connect(self.enable)
        self.disable_button.clicked.connect(self.disable)
        self.rename_button.clicked.connect(self.rename)
        self.save_presence_button.clicked.connect(self.save_presence)
        self.publish_button.clicked.connect(self.publish_ai)
        self.brain_refresh_button.clicked.connect(self.refresh_big_brain)
        self.refresh_button.clicked.connect(self.refresh_directory)
        self.network_filter.textChanged.connect(self._apply_filter)
        self.open_chat_button.clicked.connect(self.open_selected_chat)
        self.directory.cellDoubleClicked.connect(lambda _row, _column: self.open_selected_chat())
        self.create_group_button.clicked.connect(self.create_group)
        self.conversations.itemSelectionChanged.connect(self._conversation_selected)
        self.conversation_send_button.clicked.connect(self.send_conversation_message)
        self.conversation_input.returnPressed.connect(self.send_conversation_message)

    def _load_local_state(self):
        state = shared.load_shared_notify_state(create_identity=True)
        self.enabled_label.setText("Enabled" if state.enabled else "Disabled")
        self.handle.setText(str(state.handle or "").lstrip("@"))
        self.device_label.setText(state.device_id or "-")
        self.endpoint_label.setText(state.endpoint_id or "-")
        self.status_text.setText(state.status_text or "")
        self.accept_human.setChecked(state.accept_human_chat)
        self.accept_ai.setChecked(state.accept_ai_chat)
        self.accept_tasks.setChecked(state.accept_tasks)
        self._load_models()

    def _load_models(self):
        current = self.ai_model.currentText()
        try:
            rows = linked_account_catalog().get("models", [])
            models = sorted({str(row.get("id") or "") for row in rows if row.get("id")})
        except Exception:
            models = []
        self.ai_model.clear()
        self.ai_model.addItems(models)
        if current:
            self.ai_model.setCurrentText(current)

    def _run(self, operation: Callable[[], Any], success: Callable[[Any], None]):
        if self._busy:
            return
        self._busy = True
        worker = _NetworkWorker(operation, self)
        self._workers.add(worker)
        worker.success.connect(success)
        worker.error.connect(self._error)
        worker.finished.connect(lambda w=worker: self._finished(w))
        worker.start()

    def _finished(self, worker):
        self._workers.discard(worker)
        self._busy = False

    def _error(self, message: str):
        QMessageBox.warning(self, "Shared Notify", message)

    def enable(self):
        self._run(lambda: shared.enable_shared_notify(self.handle.text().strip()), self._after_identity)

    def disable(self):
        self._run(shared.disable_shared_notify, lambda _v: self._after_identity({}))

    def rename(self):
        handle = self.handle.text().strip()
        def operation():
            state = shared.load_shared_notify_state(create_identity=False)
            if not state.enabled or not state.endpoint_id:
                raise RuntimeError("Enable Shared Notify first")
            result = shared._client().notify_rename(state.endpoint_id, handle)
            endpoint = result.get("endpoint") or {}
            state.handle = str(endpoint.get("handle") or state.handle)
            shared.save_shared_notify_state(state)
            return endpoint
        self._run(operation, self._after_identity)

    def _after_identity(self, _endpoint):
        self._load_local_state()
        self.refresh_directory()

    def save_presence(self):
        kwargs = {
            "availability": self.availability.currentText(),
            "activity": self.activity.currentText(),
            "status_text": self.status_text.text().strip(),
            "accept_human_chat": self.accept_human.isChecked(),
            "accept_ai_chat": self.accept_ai.isChecked(),
            "accept_tasks": self.accept_tasks.isChecked(),
        }
        self._run(lambda: shared.set_presence(**kwargs), lambda _v: self.refresh_directory())

    def publish_ai(self):
        handle = self.ai_handle.text().strip()
        model = self.ai_model.currentText().strip()
        self._run(lambda: shared.publish_ai(handle, model), lambda _v: self.refresh_directory())

    def refresh_big_brain(self):
        state = shared.load_shared_notify_state(create_identity=False)
        if not state.enabled:
            self.brain_status.setText("Shared Notify disabled")
            return
        self._run(lambda: shared._client().notify_status(), self._show_big_brain)

    def _show_big_brain(self, result):
        memory = dict((result or {}).get("episodic_memory") or {})
        if not memory:
            self.brain_status.setText("Big Brain status unavailable")
            return
        metrics = dict(memory.get("metrics") or {})
        healthy = bool(memory.get("healthy"))
        provider = str(memory.get("provider") or "memory")
        recalls = int(metrics.get("recalls") or 0)
        hits = int(metrics.get("hits") or 0)
        injected = int(metrics.get("injected") or 0)
        latency = float(metrics.get("latency_ms") or 0.0)
        self.brain_status.setText(
            f"{'Connected' if healthy else 'Degraded'} · {provider} · "
            f"recalls {recalls} · hits {hits} · injected {injected} · latency {latency:.1f} ms"
        )

    def refresh_directory(self):
        state = shared.load_shared_notify_state(create_identity=False)
        if not state.enabled:
            self.directory_status.setText("Shared Notify disabled")
            self.directory.setRowCount(0)
            return
        def operation():
            client = shared._client()
            return {
                "directory": client.notify_directory(include_offline=True),
                "status": client.notify_status(),
                "conversations": client.notify_conversations(state.endpoint_id),
            }
        self._run(operation, self._show_network_refresh)

    def _show_network_refresh(self, result):
        self._show_big_brain((result or {}).get("status") or {})
        self._show_directory((result or {}).get("directory") or {})
        self._show_conversations((result or {}).get("conversations") or {})

    def _show_directory(self, result):
        rows = list((result or {}).get("endpoints") or [])
        self._directory_rows = rows
        self._render_directory(rows)

    def _render_directory(self, rows):
        self.directory.setRowCount(len(rows))
        for r, row in enumerate(rows):
            values = [
                ("● " if row.get("online") else "○ ") + str(row.get("handle", "")), row.get("kind", ""), row.get("availability", ""),
                row.get("activity", ""), "yes" if row.get("accept_human_chat") else "no",
                "yes" if row.get("accept_ai_chat") else "no", ", ".join(row.get("capabilities") or []),
            ]
            for c, value in enumerate(values):
                self.directory.setItem(r, c, QTableWidgetItem(str(value)))
        online = sum(1 for row in rows if row.get("online"))
        self.directory_status.setText(f"{online} online · {len(rows)} visible")
        self._busy = False

    def _show_conversations(self, result):
        self._conversation_rows = list((result or {}).get("conversations") or [])
        self._render_conversations(self._conversation_rows)

    def _render_conversations(self, rows):
        selected = self._active_conversation_id
        self.conversations.blockSignals(True)
        self.conversations.clear()
        selected_item = None
        for row in rows:
            members = [str(m.get("handle") or "") for m in row.get("members") or []]
            title = str(row.get("title") or "").strip() or ", ".join(members)
            unread = int(self._unread.get(str(row.get("conversation_id") or ""), 0))
            badge = f"  [{unread}]" if unread else ""
            item = QListWidgetItem(f"{title}  ·  {len(members)}{badge}")
            item.setData(Qt.ItemDataRole.UserRole, str(row.get("conversation_id") or ""))
            item.setToolTip(" · ".join(members))
            self.conversations.addItem(item)
            if item.data(Qt.ItemDataRole.UserRole) == selected:
                selected_item = item
        self.conversations.blockSignals(False)
        if selected_item is not None:
            self.conversations.setCurrentItem(selected_item)

    def _apply_filter(self, text):
        needle = str(text or "").strip().lower()
        directory = self._directory_rows
        conversations = self._conversation_rows
        if needle:
            directory = [row for row in directory if needle in " ".join([
                str(row.get("handle") or ""), str(row.get("label") or ""),
                str(row.get("kind") or ""), " ".join(row.get("capabilities") or []),
            ]).lower()]
            conversations = [row for row in conversations if needle in " ".join([
                str(row.get("title") or ""),
                *[str(m.get("handle") or "") for m in row.get("members") or []],
            ]).lower()]
        self._render_directory(directory)
        self._render_conversations(conversations)

    def _selected_endpoint_handles(self):
        handles = []
        for index in self.directory.selectionModel().selectedRows():
            item = self.directory.item(index.row(), 0)
            if item and item.text().strip():
                handles.append(item.text().strip().lstrip("●○ "))
        return handles

    def open_selected_chat(self):
        handles = self._selected_endpoint_handles()
        if len(handles) != 1:
            QMessageBox.information(self, "Messenger", "Select exactly one endpoint in the AI Network table.")
            return
        self._create_conversation(handles, kind="direct")

    def create_group(self):
        handles = self._selected_endpoint_handles()
        if not handles:
            QMessageBox.information(self, "Messenger", "Select one or more endpoints in the AI Network table.")
            return
        title, ok = QInputDialog.getText(self, "Create Group", "Conversation title:")
        if not ok:
            return
        self._create_conversation(handles, kind="group", title=title.strip())

    def _create_conversation(self, handles, *, kind, title=""):
        state = shared.load_shared_notify_state(create_identity=False)
        if not state.enabled or not state.endpoint_id:
            QMessageBox.warning(self, "Messenger", "Enable Shared Notify first.")
            return
        clean = [str(h).strip() for h in handles if str(h).strip() and str(h).strip().lstrip("@") != state.handle.lstrip("@")]
        if not clean:
            QMessageBox.information(self, "Messenger", "Select another endpoint, not this client itself.")
            return
        def operation():
            client = shared._client()
            result = client.notify_conversation_create(title, state.endpoint_id, clean, kind=kind)
            return {"created": result, "conversations": client.notify_conversations(state.endpoint_id)}
        self._run(operation, self._after_conversation_created)

    def _after_conversation_created(self, result):
        created = ((result or {}).get("created") or {}).get("conversation") or {}
        self._active_conversation_id = str(created.get("conversation_id") or "")
        self._show_conversations((result or {}).get("conversations") or {})
        self._load_active_conversation()

    def _conversation_selected(self):
        item = self.conversations.currentItem()
        if item is None:
            return
        self._active_conversation_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
        self._unread.pop(self._active_conversation_id, None)
        self._render_conversations(self._conversation_rows)
        self._load_active_conversation()

    def _load_active_conversation(self):
        cid = self._active_conversation_id
        if not cid:
            return
        self._run(lambda: shared._client().notify_conversation_history(cid), self._show_conversation_history)

    def _show_conversation_history(self, result):
        import html
        messages = list((result or {}).get("messages") or [])
        cid = self._active_conversation_id
        signature = tuple(str(m.get("message_id") or m.get("correlation_id") or "") for m in messages)
        self._last_history_signature[cid] = signature
        row = next((r for r in self._conversation_rows if str(r.get("conversation_id") or "") == cid), {})
        members = ", ".join(str(m.get("handle") or "") for m in row.get("members") or [])
        self.conversation_title.setText(f"{row.get('title') or 'Conversation'} · {members}")
        self.conversation_log.clear()
        for message in messages:
            sender = str(message.get("sender_handle") or "@unknown")
            kind = str(message.get("kind") or "chat")
            title = str(message.get("title") or "")
            body = str(message.get("body") or "")
            count = int(message.get("delivery_count") or 1)
            prefix = f"{sender} [{kind}]" + (f" · {count} deliveries" if count > 1 else "")
            if title:
                prefix += f" · {title}"
            safe_body = html.escape(body).replace("\n", "<br>")
            state = shared.load_shared_notify_state(create_identity=False)
            me = str(state.handle or "").lstrip("@")
            if me:
                safe_body = safe_body.replace(f"@{html.escape(me)}", f"<b>@{html.escape(me)}</b>")
            self.conversation_log.append(f"<b>{html.escape(prefix)}</b><br>{safe_body}")

    def send_conversation_message(self):
        cid = self._active_conversation_id
        body = self.conversation_input.text().strip()
        if not cid or not body:
            return
        state = shared.load_shared_notify_state(create_identity=False)
        payload = {"sender_endpoint_id": state.endpoint_id, "kind": "human_chat", "body": body}
        def operation():
            client = shared._client()
            client.notify_conversation_send(cid, payload)
            return client.notify_conversation_history(cid)
        self.conversation_input.clear()
        self._run(operation, self._show_conversation_history)

    def _refresh_if_visible(self):
        incoming = shared.drain_received_messages()
        active_changed = False
        for message in incoming:
            metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
            cid = str(metadata.get("conversation_id") or "")
            if not cid:
                continue
            if cid == self._active_conversation_id and self.isVisible():
                active_changed = True
            else:
                self._unread[cid] = self._unread.get(cid, 0) + 1
        if incoming:
            self._render_conversations(self._conversation_rows)
        if active_changed and not self._busy:
            self._load_active_conversation()
        elif self.isVisible() and not self._busy:
            self.refresh_directory()
