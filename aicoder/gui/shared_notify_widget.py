"""Shared Notify / Presence UI for the account-scoped AILinux AI network."""
from __future__ import annotations

from typing import Any, Callable

from PyQt6.QtCore import QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
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
        self.directory.horizontalHeader().setStretchLastSection(True)
        dlayout.addWidget(self.directory)
        root.addWidget(directory_box, 1)

        self.enable_button.clicked.connect(self.enable)
        self.disable_button.clicked.connect(self.disable)
        self.rename_button.clicked.connect(self.rename)
        self.save_presence_button.clicked.connect(self.save_presence)
        self.publish_button.clicked.connect(self.publish_ai)
        self.refresh_button.clicked.connect(self.refresh_directory)

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

    def refresh_directory(self):
        state = shared.load_shared_notify_state(create_identity=False)
        if not state.enabled:
            self.directory_status.setText("Shared Notify disabled")
            self.directory.setRowCount(0)
            return
        self._run(lambda: shared._client().notify_directory(include_offline=True), self._show_directory)

    def _show_directory(self, result):
        rows = list((result or {}).get("endpoints") or [])
        self.directory.setRowCount(len(rows))
        for r, row in enumerate(rows):
            values = [
                row.get("handle", ""), row.get("kind", ""), row.get("availability", ""),
                row.get("activity", ""), "yes" if row.get("accept_human_chat") else "no",
                "yes" if row.get("accept_ai_chat") else "no", ", ".join(row.get("capabilities") or []),
            ]
            for c, value in enumerate(values):
                self.directory.setItem(r, c, QTableWidgetItem(str(value)))
        online = sum(1 for row in rows if row.get("online"))
        self.directory_status.setText(f"{online} online · {len(rows)} visible")
        self._busy = False

    def _refresh_if_visible(self):
        if self.isVisible() and not self._busy:
            self.refresh_directory()
