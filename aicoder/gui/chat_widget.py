"""Chat widget with agent loop, command approval, stop button and audit."""
from __future__ import annotations
import html
import json
import threading
import time
from datetime import datetime

try:
    import markdown as _md
    _HAS_MD = True
except ImportError:
    _HAS_MD = False

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QTextEdit, QLineEdit, QPushButton, QLabel, QMessageBox, QComboBox,
    QMenu, QInputDialog,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QMetaObject, Q_ARG, QMimeData, QUrl
from PyQt6.QtGui import QTextCursor, QDragEnterEvent, QDropEvent

from ..config import load_session
from ..session_state import get_state
from ..client import TriForceClient, ClientError
from .. import chat_history
from ..executor import (
    load_tools, build_system_prompt, build_tool_desc,
    parse_tool_calls, strip_tool_calls, trim_messages, run_tool,
    is_destructive, MAX_ITERATIONS,
)


class _AgentWorker(QThread):
    """Background thread: agent loop with approval support and stop."""
    msg = pyqtSignal(str, str, str)          # (role, text, meta)
    finished = pyqtSignal(str, str)           # (final_text, model)
    error = pyqtSignal(str)
    messages_updated = pyqtSignal(list)
    approval_needed = pyqtSignal(str, str)    # (tool_name, command_preview)

    def __init__(self, client, messages_array, model, fallback, tools, system_prompt):
        super().__init__()
        self.client = client
        self.messages = list(messages_array)
        self.model = model
        self.fallback = fallback
        self.tools = tools
        self.system = system_prompt
        # Approval mechanism: threading.Event + result flag
        self._approval_event = threading.Event()
        self._approval_result = False
        self._stopped = False

    def stop(self):
        """Request stop from main thread."""
        self._stopped = True

    def set_approval(self, approved: bool):
        """Called from main thread after user decision."""
        self._approval_result = approved
        self._approval_event.set()

    def _gui_approval(self, tool_name: str, args: dict) -> bool:
        """Approval callback for local_exec — blocks until user decides or stop."""
        cmd = args.get("command", "")
        # Emit signal to main thread, then wait
        self._approval_event.clear()
        self._approval_result = False
        self.approval_needed.emit(tool_name, cmd)
        # Poll every 2s so stop() is respected during pending approval
        for _ in range(60):  # max 120s total
            if self._approval_event.wait(timeout=2):
                break
            if self._stopped:
                return False  # Agent stopped — reject command
        return self._approval_result

    def run(self):
        messages = self.messages
        model_used = self.model or "default"
        MAX_CTX = 30

        for i in range(MAX_ITERATIONS):
            if self._stopped:
                self.finished.emit("(Agent stopped)", model_used)
                return

            messages = trim_messages(messages)

            try:
                result = self.client.chat(
                    messages=messages,
                    model=self.model or None,
                    fallback_model=self.fallback or None,
                    temperature=0.3,
                    max_tokens=4096,
                )
            except (ClientError, Exception) as e:
                self.error.emit(str(e))
                return

            response = result.get("response", "").strip()
            model_used = result.get("model", self.model or "default")

            calls = parse_tool_calls(response)
            visible = strip_tool_calls(response)

            if not calls:
                messages.append({"role": "assistant", "content": response})
                self.messages_updated.emit(messages)
                self.finished.emit(response, model_used)
                return

            if visible:
                self.msg.emit("thought", visible, f"step {i+1}")

            # Tool execution
            tool_results = []
            for call in calls:
                if self._stopped:
                    self.messages_updated.emit(messages)
                    self.finished.emit("(Agent stopped)", model_used)
                    return

                tname = call.get("name", "?")
                targs = call.get("arguments", {})
                self.msg.emit("tool", f">> {tname}({json.dumps(targs, ensure_ascii=False)[:200]})", "")

                t0 = time.time()
                tr, is_err = run_tool(
                    self.client, tname, targs,
                    approval_fn=self._gui_approval,
                    model=model_used,
                    iteration=i,
                )
                elapsed = time.time() - t0

                status = f"{'ERROR' if is_err else 'OK'} ({elapsed:.1f}s)"
                self.msg.emit("tool_result", tr[:2000], f"{tname} {status}")
                tool_results.append(f"Tool {tname} result:\n{tr}")

            messages.append({"role": "assistant", "content": response})
            current_input = "\n\n".join(tool_results)
            messages.append({"role": "user", "content": current_input})

            if "DONE:" in response[:200].upper():
                self.messages_updated.emit(messages)
                self.finished.emit(visible or response, model_used)
                return

        self.messages_updated.emit(messages)
        self.finished.emit(f"(Max {MAX_ITERATIONS} iterations)\n{visible or response}", model_used)


class ChatWidget(QWidget):
    def __init__(self, settings_ref=None, parent=None):
        super().__init__(parent)
        self.settings_ref = settings_ref
        self._worker = None
        self._tools = None
        self._system = None
        self._messages = []
        self._syncing = False
        self._session_id = None
        self._dropped_files = []
        self._build_ui()
        self.setAcceptDrops(True)
        # Connect to settings model list + selection changes
        if self.settings_ref:
            if hasattr(self.settings_ref, "models_loaded"):
                self.settings_ref.models_loaded.connect(self._on_models_updated)
            if hasattr(self.settings_ref, "selection_changed"):
                self.settings_ref.selection_changed.connect(self._on_settings_selection_changed)

    def _on_models_updated(self, models: list):
        """Update model dropdowns with list from backend."""
        self._syncing = True
        self.model_combo.clear()
        self.fallback_combo.clear()
        self.model_combo.addItem("")    # Backend-Default
        self.fallback_combo.addItem("")  # kein Fallback
        for m in models:
            self.model_combo.addItem(m)
            self.fallback_combo.addItem(m)
        # Sync selection from settings (if user hasn't overridden)
        if self.settings_ref:
            self.model_combo.setCurrentText(self.settings_ref.get_current_model())
            self.fallback_combo.setCurrentText(self.settings_ref.get_current_fallback())
        self._syncing = False

    def _on_settings_selection_changed(self, model: str, fallback: str):
        """Settings tab selection changed — sync chat tab."""
        self._syncing = True
        self.model_combo.setCurrentText(model)
        self.fallback_combo.setCurrentText(fallback)
        self._syncing = False

    # ── Drag & Drop ──────────────────────────────────────────────
    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls() or event.mimeData().hasText():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        mime = event.mimeData()
        if mime.hasUrls():
            for url in mime.urls():
                path = url.toLocalFile()
                if path:
                    self._handle_dropped_file(path)
        elif mime.hasText():
            text = mime.text().strip()
            if text:
                self.input.setText(text)
        event.acceptProposedAction()

    def _handle_dropped_file(self, path: str):
        """Load dropped file as context for next message."""
        import os
        name = os.path.basename(path)
        try:
            size = os.path.getsize(path)
            if size > 500_000:
                self._append_msg("system", f"File too large: {name} ({size//1024}KB, max 500KB)")
                return
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(100_000)
            self._dropped_files.append({"name": name, "path": path, "content": content})
            self._append_msg("system", f"📎 {name} ({size//1024}KB) — wird als Context mitgesendet")
        except Exception as e:
            self._append_msg("error", f"Konnte {name} nicht laden: {e}")

    # ── History ──────────────────────────────────────────────────
    def _show_history_menu(self):
        """Show recent sessions as context menu."""
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background: #1a1a2e; color: #ccc; border: 1px solid #444; }"
            "QMenu::item:selected { background: #2a2a4e; }"
        )
        sessions = chat_history.list_sessions(limit=15)
        if not sessions:
            menu.addAction("(keine Sessions)").setEnabled(False)
        else:
            for s in sessions:
                title = s["title"][:40]
                ts = s["updated_at"][:10] if s.get("updated_at") else ""
                action = menu.addAction(f"{title}  ({ts})")
                action.setData(s["id"])
        menu.addSeparator()
        new_action = menu.addAction("➕ Neue Session")
        chosen = menu.exec(self.history_btn.mapToGlobal(self.history_btn.rect().bottomLeft()))
        if chosen == new_action:
            self._new_session()
        elif chosen and chosen.data():
            self._load_session(chosen.data())

    def _new_session(self):
        self._clear_chat()
        self._session_id = None

    def _load_session(self, session_id: str):
        """Restore a previous chat session."""
        self.log.clear()
        self._messages = []
        self._session_id = session_id
        msgs = chat_history.load_messages(session_id)
        for m in msgs:
            role = m["role"]
            if role == "system":
                continue
            self._append_msg(role, m["content"], m.get("meta", ""))
            self._messages.append({"role": role, "content": m["content"]})
        self._update_status_idle("Session geladen")

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Chat-Log
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setStyleSheet("""
            QTextEdit {
                background: #0a0a1a; color: #e0e0e0;
                font-family: 'Cascadia Code', 'Fira Code', 'Consolas', monospace;
                font-size: 13px; border: 1px solid #333;
                border-radius: 4px; padding: 8px;
            }
        """)
        layout.addWidget(self.log, stretch=1)

        # Model-Selector Row
        model_row = QHBoxLayout()
        model_row.setSpacing(6)

        model_label = QLabel("Model:")
        model_label.setStyleSheet("color: #888; font-size: 11px;")
        model_label.setFixedWidth(45)
        model_row.addWidget(model_label)

        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setStyleSheet("""
            QComboBox {
                background: #111; color: #ccc; border: 1px solid #333;
                border-radius: 3px; padding: 3px 8px; font-size: 11px;
            }
            QComboBox:focus { border-color: #00d4ff; }
            QComboBox QAbstractItemView {
                background: #111; color: #ccc; selection-background-color: #1a3a5e;
            }
        """)
        self.model_combo.addItem("")  # Backend-Default (liste wird dynamisch geladen)
        self.model_combo.setCurrentText("")
        self.model_combo.setToolTip("Select model (empty = backend default)")
        model_row.addWidget(self.model_combo, stretch=1)

        fb_label = QLabel("Fallback:")
        fb_label.setStyleSheet("color: #666; font-size: 11px;")
        fb_label.setFixedWidth(55)
        model_row.addWidget(fb_label)

        self.fallback_combo = QComboBox()
        self.fallback_combo.setEditable(True)
        self.fallback_combo.setStyleSheet("""
            QComboBox {
                background: #111; color: #999; border: 1px solid #333;
                border-radius: 3px; padding: 3px 8px; font-size: 11px;
            }
            QComboBox QAbstractItemView {
                background: #111; color: #ccc; selection-background-color: #1a3a5e;
            }
        """)
        self.fallback_combo.addItem("")  # (liste wird dynamisch geladen)
        self.fallback_combo.setCurrentText("")
        self.fallback_combo.setToolTip("Fallback model (optional)")
        model_row.addWidget(self.fallback_combo, stretch=1)

        layout.addLayout(model_row)

        # Status-Zeile (erweitert: User, Tier, Workspace, Tools)
        self.status = QLabel("Ready.")
        self.status.setStyleSheet("color: #888; font-size: 11px;")
        layout.addWidget(self.status)

        # Input-Zeile
        input_row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("Enter message... (Enter to send)")
        self.input.setStyleSheet("""
            QLineEdit {
                background: #111; color: #fff;
                border: 1px solid #444; border-radius: 4px;
                padding: 8px 12px; font-size: 13px;
            }
            QLineEdit:focus { border-color: #00d4ff; }
        """)
        self.input.returnPressed.connect(self._send)
        input_row.addWidget(self.input, stretch=1)

        self.send_btn = QPushButton("Send")
        self.send_btn.setStyleSheet("""
            QPushButton {
                background: #00d4ff; color: #000; border: none;
                border-radius: 4px; padding: 8px 18px; font-weight: bold;
            }
            QPushButton:hover { background: #00b8e6; }
            QPushButton:disabled { background: #555; color: #999; }
        """)
        self.send_btn.clicked.connect(self._send)
        input_row.addWidget(self.send_btn)

        # Stop-Button — bricht laufenden Agent-Loop ab
        self.stop_btn = QPushButton("■")
        self.stop_btn.setToolTip("Stop agent")
        self.stop_btn.setFixedWidth(38)
        self.stop_btn.setStyleSheet("""
            QPushButton {
                background: #1a1a2e; color: #ff6b6b;
                border: 1px solid #444; border-radius: 4px;
                font-size: 14px; padding: 4px; font-weight: bold;
            }
            QPushButton:hover { background: #3a1a1a; border-color: #ff6b6b; }
            QPushButton:disabled { color: #555; border-color: #333; }
        """)
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop_agent)
        input_row.addWidget(self.stop_btn)

        # Clear-Button
        self.clear_btn = QPushButton("↺")
        self.clear_btn.setToolTip("Reset chat & context")
        self.clear_btn.setFixedWidth(38)
        self.clear_btn.setStyleSheet("""
            QPushButton {
                background: #1a1a2e; color: #888;
                border: 1px solid #444; border-radius: 4px;
                font-size: 16px; padding: 4px;
            }
            QPushButton:hover { background: #2a2a3e; color: #ff9800; border-color: #ff9800; }
        """)
        self.clear_btn.clicked.connect(self._clear_chat)
        input_row.addWidget(self.clear_btn)

        # History-Button
        self.history_btn = QPushButton("📋")
        self.history_btn.setToolTip("Chat-Verlauf")
        self.history_btn.setFixedWidth(38)
        self.history_btn.setStyleSheet("""
            QPushButton {
                background: #1a1a2e; color: #888;
                border: 1px solid #444; border-radius: 4px;
                font-size: 14px; padding: 4px;
            }
            QPushButton:hover { background: #2a2a3e; color: #00d4ff; border-color: #00d4ff; }
        """)
        self.history_btn.clicked.connect(self._show_history_menu)
        input_row.addWidget(self.history_btn)

        layout.addLayout(input_row)

    def _append_msg(self, role: str, text: str, meta: str = ""):
        ts = datetime.now().strftime("%H:%M:%S")
        colors = {
            "user": ("#00d4ff", "You"),
            "assistant": ("#00ff88", "AI"),
            "thought": ("#888", "Thought"),
            "tool": ("#ff9800", "Tool"),
            "tool_result": ("#aaa", "Result"),
            "error": ("#ff6b6b", "Error"),
            "system": ("#666", "System"),
        }
        color, label = colors.get(role, ("#ccc", role))
        meta_html = f' <span style="color:#666;">({html.escape(meta)})</span>' if meta else ""
        if role in ("assistant", "thought") and _HAS_MD:
            body = _md.markdown(text, extensions=["fenced_code", "nl2br", "tables"])
            # Style code blocks
            body = body.replace(
                "<code>",
                '<code style="background:#1a1a3e;color:#ff9800;padding:1px 4px;border-radius:3px;font-size:12px;">'
            )
            body = body.replace(
                "<pre>",
                '<pre style="background:#0d0d2a;border:1px solid #333;border-radius:6px;'
                'padding:10px;overflow-x:auto;font-size:12px;line-height:1.4;">'
            )
            content_html = f'<div style="color:#e0e0e0;">{body}</div>'
        else:
            esc = html.escape(text)
            content_html = f'<span style="color:#e0e0e0; white-space:pre-wrap;">{esc}</span>'
        block = (
            f'<div style="margin:4px 0;">'
            f'<span style="color:#666;">[{ts}]</span> '
            f'<span style="color:{color};font-weight:bold;">{label}</span>{meta_html}<br>'
            f'{content_html}'
            f'</div><hr style="border-color:#222;">'
        )
        self.log.moveCursor(QTextCursor.MoveOperation.End)
        self.log.insertHtml(block)
        self.log.moveCursor(QTextCursor.MoveOperation.End)

    def _update_status_idle(self, extra: str = ""):
        """Update status bar with session info + token status."""
        parts = []
        try:
            session = load_session()
            client = TriForceClient(session.base_url, token=session.token, timeout=5)
            parts.append(session.user_id or "?")
            parts.append(session.tier or "?")
            # Token expiry status
            tok_status = client.token_status()
            parts.append(f"Token: {tok_status}")
        except Exception:
            parts.append("not logged in")
        state = get_state()
        ws = state.get("workspace_root")
        if ws:
            from pathlib import Path
            parts.append(Path(ws).name)
        if self._tools:
            parts.append(f"{len(self._tools)} Tools")
        if extra:
            parts.append(extra)
        self.status.setText(" · ".join(parts))
        # Color based on token status
        color = "#888"
        if "expired" in " ".join(parts):
            color = "#ff6b6b"
        elif "expires in" in " ".join(parts):
            color = "#ff9800"
        self.status.setStyleSheet(f"color: {color}; font-size: 11px;")

    def _clear_chat(self):
        self.log.clear()
        self._tools = None
        self._system = None
        self._messages = []
        self._update_status_idle("Reset")
        self._append_msg("system", "Chat and context reset.", "")

    def _stop_agent(self):
        if self._worker and self._worker.isRunning():
            self._worker.stop()
            self._append_msg("system", "Stop requested...", "")

    def _on_approval_needed(self, tool_name: str, command: str):
        """Show modal dialog for command approval (main thread)."""
        preview = command if len(command) <= 300 else command[:300] + "…"
        destructive = is_destructive(command)
        title = "⚠️ Destructive Command" if destructive else "local_exec"
        msg = (
            f"The agent wants to run the following command locally:\n\n"
            f"{preview}\n\n"
            f"{'⚠️ WARNING: Potentially destructive!' if destructive else ''}\n"
            f"Execute?"
        )
        reply = QMessageBox.question(
            self, title, msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No if destructive else QMessageBox.StandardButton.Yes,
        )
        approved = reply == QMessageBox.StandardButton.Yes
        if self._worker:
            self._worker.set_approval(approved)
        if not approved:
            self._append_msg("system", f"Command rejected: {preview[:100]}", "")

    def _send(self):
        text = self.input.text().strip()
        if not text:
            return
        self._append_msg("user", text)
        self.input.clear()
        self.send_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status.setText("Agent working...")
        self.status.setStyleSheet("color: #00d4ff; font-size: 11px;")

        try:
            session = load_session()
            client = TriForceClient(session.base_url, token=session.token, timeout=120)
        except Exception as e:
            self._append_msg("error", f"Keine Session: {e}")
            self.send_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            return

        # Load tools once per session
        if self._tools is None:
            self._append_msg("system", "Loading MCP tools...", "")
            try:
                self._tools = load_tools(client)
                state = get_state()
                self._system = build_system_prompt(
                    self._tools,
                    state.get("workspace_root"),
                )
                self._append_msg("system", f"{len(self._tools)} tools loaded", "")
            except Exception as e:
                self._append_msg("error", f"Tool-Loading: {e}")
                self._tools = []
                self._system = (
                    "Du bist ai-coder, autonomer Coding- und DevOps-Agent auf AILinux/TriForce (api.ailinux.me). "
                    "INIT: current_time pruefen, memory_search, dann handeln. "
                    "Lesen vor Schreiben. Diagnose vor Patch. Kleinste Aenderung zuerst. Sprache: Deutsch."
                )

        # Priority: combo box > settings tab > state file
        model = self.model_combo.currentText().strip()
        fallback = self.fallback_combo.currentText().strip()
        state = get_state()
        if not model and self.settings_ref:
            model = self.settings_ref.get_current_model()
        if not fallback and self.settings_ref:
            fallback = self.settings_ref.get_current_fallback()
        if not model:
            model = state.get("selected_model", "")
        if not fallback:
            fallback = state.get("fallback_model", "")

        if not self._messages:
            self._messages = [{"role": "system", "content": self._system}]

        # Attach dropped files as context
        user_content = text
        if self._dropped_files:
            file_ctx = []
            for f in self._dropped_files:
                file_ctx.append(f"--- FILE: {f['name']} ---\n{f['content'][:50000]}\n--- END ---")
            user_content = "\n\n".join(file_ctx) + "\n\nUser message: " + text
            self._dropped_files.clear()

        self._messages.append({"role": "user", "content": user_content})

        # Save to history
        if not self._session_id:
            title = text[:50].strip() or "New Chat"
            self._session_id = chat_history.create_session(title=title)
        chat_history.save_message(self._session_id, "user", text)

        self._worker = _AgentWorker(client, self._messages, model, fallback, self._tools, self._system)
        self._worker.msg.connect(self._on_agent_msg)
        self._worker.finished.connect(self._on_response)
        self._worker.messages_updated.connect(self._on_messages_updated)
        self._worker.error.connect(self._on_error)
        self._worker.approval_needed.connect(self._on_approval_needed)
        self._worker.start()

    def _on_agent_msg(self, role: str, text: str, meta: str):
        self._append_msg(role, text, meta)

    def _on_response(self, text: str, model_used: str):
        self._append_msg("assistant", text, model_used)
        if self._session_id:
            chat_history.save_message(self._session_id, "assistant", text, model_used)
        self.send_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._update_status_idle(f"Done ({model_used})")

    def _on_messages_updated(self, messages: list):
        self._messages = messages

    def _on_error(self, err: str):
        self._append_msg("error", err)
        self.send_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._update_status_idle("Error")
