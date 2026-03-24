"""Chat-Widget fuer ai-coder GUI — Log + Input + threaded API-Call."""
from __future__ import annotations
import html
from datetime import datetime
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QTextEdit, QLineEdit, QPushButton, QLabel,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QTextCursor

from ..config import load_session
from ..session_state import get_state
from ..client import TriForceClient, ClientError

DEFAULT_SYSTEM_PROMPT = (
    "Du bist ai-coder, ein hilfreicher Coding- und DevOps-Assistent von AILinux. "
    "Antworte praezise und direkt. Bei Code-Fragen: gib lauffaehigen Code. "
    "Bei DevOps: konkrete Befehle. Kein Smalltalk, keine Wiederholungen. "
    "Sprache: Deutsch, ausser der User schreibt Englisch."
)


class _ChatWorker(QThread):
    """Background-Thread fuer API-Call."""
    finished = pyqtSignal(str, str)   # (response_text, model_used)
    error = pyqtSignal(str)

    def __init__(self, client, message, model, fallback, system_prompt):
        super().__init__()
        self.client = client
        self.message = message
        self.model = model
        self.fallback = fallback
        self.system_prompt = system_prompt

    def run(self):
        try:
            result = self.client.chat(
                message=self.message,
                model=self.model or None,
                fallback_model=self.fallback or None,
                system_prompt=self.system_prompt or None,
            )
            text = result.get("response", result.get("content", str(result)))
            model_used = result.get("model", self.model or "default")
            self.finished.emit(str(text), str(model_used))
        except (ClientError, Exception) as e:
            self.error.emit(str(e))


class ChatWidget(QWidget):
    def __init__(self, settings_ref=None, parent=None):
        super().__init__(parent)
        self.settings_ref = settings_ref
        self._worker = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Chat-Log
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setStyleSheet("""
            QTextEdit {
                background: #0a0a1a;
                color: #e0e0e0;
                font-family: 'Cascadia Code', 'Fira Code', 'Consolas', monospace;
                font-size: 13px;
                border: 1px solid #333;
                border-radius: 4px;
                padding: 8px;
            }
        """)
        layout.addWidget(self.log, stretch=1)

        # Status-Zeile
        self.status = QLabel("Bereit.")
        self.status.setStyleSheet("color: #888; font-size: 11px;")
        layout.addWidget(self.status)

        # Input-Zeile
        input_row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("Nachricht eingeben... (Enter zum Senden)")
        self.input.setStyleSheet("""
            QLineEdit {
                background: #111;
                color: #fff;
                border: 1px solid #444;
                border-radius: 4px;
                padding: 8px 12px;
                font-size: 13px;
            }
            QLineEdit:focus { border-color: #00d4ff; }
        """)
        self.input.returnPressed.connect(self._send)
        input_row.addWidget(self.input, stretch=1)

        self.send_btn = QPushButton("Senden")
        self.send_btn.setStyleSheet("""
            QPushButton {
                background: #00d4ff;
                color: #000;
                border: none;
                border-radius: 4px;
                padding: 8px 18px;
                font-weight: bold;
            }
            QPushButton:hover { background: #00b8e6; }
            QPushButton:disabled { background: #555; color: #999; }
        """)
        self.send_btn.clicked.connect(self._send)
        input_row.addWidget(self.send_btn)

        layout.addLayout(input_row)

    def _append_msg(self, role: str, text: str, meta: str = ""):
        ts = datetime.now().strftime("%H:%M:%S")
        esc = html.escape(text)
        if role == "user":
            color = "#00d4ff"
            label = "Du"
        elif role == "assistant":
            color = "#00ff88"
            label = "AI"
        else:
            color = "#ff6b6b"
            label = role
        meta_html = f' <span style="color:#666;">({html.escape(meta)})</span>' if meta else ""
        block = (
            f'<div style="margin:4px 0;">'
            f'<span style="color:#666;">[{ts}]</span> '
            f'<span style="color:{color};font-weight:bold;">{label}</span>{meta_html}<br>'
            f'<span style="color:#e0e0e0; white-space:pre-wrap;">{esc}</span>'
            f'</div><hr style="border-color:#222;">'
        )
        self.log.append(block)
        self.log.moveCursor(QTextCursor.MoveOperation.End)

    def _send(self):
        text = self.input.text().strip()
        if not text:
            return

        self._append_msg("user", text)
        self.input.clear()
        self.send_btn.setEnabled(False)
        self.status.setText("Sende...")
        self.status.setStyleSheet("color: #00d4ff; font-size: 11px;")

        # Client aus Session
        try:
            session = load_session()
            client = TriForceClient(session.base_url, token=session.token)
        except Exception as e:
            self._append_msg("error", f"Keine Session: {e}")
            self.send_btn.setEnabled(True)
            self.status.setText("Nicht eingeloggt.")
            self.status.setStyleSheet("color: #ff6b6b; font-size: 11px;")
            return

        state = get_state()
        model = ""
        fallback = ""
        if self.settings_ref:
            model = self.settings_ref.get_current_model()
            fallback = self.settings_ref.get_current_fallback()
        if not model:
            model = state.get("selected_model", "")
        if not fallback:
            fallback = state.get("fallback_model", "")

        self._worker = _ChatWorker(client, text, model, fallback, DEFAULT_SYSTEM_PROMPT)
        self._worker.finished.connect(self._on_response)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _on_response(self, text: str, model_used: str):
        self._append_msg("assistant", text, model_used)
        self.send_btn.setEnabled(True)
        self.status.setText(f"Antwort von {model_used}")
        self.status.setStyleSheet("color: #00ff88; font-size: 11px;")

    def _on_error(self, err: str):
        self._append_msg("error", err)
        self.send_btn.setEnabled(True)
        self.status.setText("Fehler.")
        self.status.setStyleSheet("color: #ff6b6b; font-size: 11px;")
