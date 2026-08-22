"""Chat widget with agent loop, command approval, stop button and audit."""
from __future__ import annotations
import html
import json
import threading
import time
from datetime import datetime
from pathlib import Path

try:
    import markdown as _md
    _HAS_MD = True
except ImportError:
    _HAS_MD = False

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QTextEdit, QPlainTextEdit, QPushButton, QLabel, QMessageBox,
    QMenu, QInputDialog, QFileDialog,
)
from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal, QMetaObject, Q_ARG, QMimeData, QUrl, QBuffer, QIODevice
from PyQt6.QtGui import (
    QTextCursor, QDragEnterEvent, QDropEvent, QKeyEvent, QKeySequence, QShortcut,
)

from ..config import load_session
from ..privileges import (
    approval_is_automatic, assess_execution, format_request,
)
from ..session_state import DEFAULT_RUNTIME_MODE, get_state
from ..workspace import active_workspace
from ..client import TriForceClient
from ..attachments import (
    Attachment, MAX_ATTACHMENTS, MAX_TOTAL_ATTACHMENT_BYTES, MAX_TOTAL_TEXT_CHARS, SUPPORTED_SUFFIXES,
    image_attachment, load_path, multimodal_content,
)
from .. import chat_history
from ..executor import (
    build_system_prompt, is_destructive, is_simple_chat_message, should_load_tools,
)


class _AgentWorker(QThread):
    """Background thread: agent loop with approval support and stop."""
    msg = pyqtSignal(str, str, str)          # (role, text, meta)
    finished = pyqtSignal(str, str)           # (final_text, model)
    error = pyqtSignal(str)
    messages_updated = pyqtSignal(list)
    approval_needed = pyqtSignal(str, object)  # tool, complete approval arguments

    def __init__(
        self, client, messages_array, model, fallback, tools, system_prompt,
        load_tools_on_start=True, enabled_tool_names=None, quick_chat=False,
    ):
        super().__init__()
        self.client = client
        self.messages = list(messages_array)
        self.model = model
        self.fallback = fallback
        self.tools = tools
        self.system = system_prompt
        self.load_tools_on_start = load_tools_on_start
        self.enabled_tool_names = enabled_tool_names
        self.quick_chat = quick_chat
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
        """Approval callback for risky workspace writes."""
        approval_args = dict(args)
        # Preserve the complete, security-enriched argument map. Structured MCP
        # writes often have no command string; dropping metadata here made the
        # GUI dialog misclassify or hide the requested operation.
        self._approval_event.clear()
        self._approval_result = False
        self.approval_needed.emit(tool_name, approval_args)
        # Poll every 2s so stop() is respected during pending approval
        for _ in range(60):  # max 120s total
            if self._approval_event.wait(timeout=2):
                break
            if self._stopped:
                return False  # Agent stopped — reject command
        return self._approval_result

    def _emit_advisor_review(self, original_request: str, response: str, operator_model: str) -> None:
        """Run the configured swarm as a non-executing post-response advisor."""
        from ..session_state import get_state
        from ..swarm_runner import should_auto_swarm

        state = get_state()
        mode = state.get("swarm_mode", "off")
        if mode == "auto" and not should_auto_swarm(original_request):
            return
        if mode not in {"auto", "on", "review"}:
            return
        advisor = self.fallback
        if not advisor or advisor == operator_model:
            return
        prompt = (
            "Act only as an advisor. Review the operator response for bugs, security "
            "risks, missing verification, and conflicts with the request. Do not call "
            "tools.\n\n"
            f"Request:\n{original_request[:4000]}\n\n"
            f"Operator response:\n{response[:12000]}"
        )
        try:
            result = self.client.chat(
                message=prompt, model=advisor, system_prompt=self.system,
                temperature=0.2, max_tokens=2048,
            )
            review = str(result.get("response", "") or "").strip()
            if review:
                self.msg.emit("system", review, f"Swarm review · {result.get('model', advisor)}")
        except Exception as exc:
            self.msg.emit("system", f"Swarm review unavailable: {exc}", "advisor")

    def run(self):
        # GUI-Resilienz: in PyQt6 killt eine unbehandelte Exception in einer
        # QThread.run()-Reimplementation den ganzen Prozess (stiller GUI-Abbruch).
        # Die REPL ueberlebt dieselben Fehler, weil sie im Main-Thread unter
        # hoeheren except-Handlern laeuft. Dasselbe Sicherheitsnetz hier:
        try:
            self._run_impl()
        except Exception as e:  # bewusster catch-all: Thread darf nie ungebremst sterben
            import traceback
            try:
                self.error.emit(f"Agent crashed: {e}\n{traceback.format_exc()}")
            except Exception:
                pass

    @staticmethod
    def _content_text(content, *, limit: int = 60_000) -> str:
        """Produce bounded text context while dropping binary image payloads."""
        if isinstance(content, str):
            return content[:limit]
        if isinstance(content, list):
            chunks = []
            image_count = 0
            remaining = max(0, int(limit))
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") in {"text", "input_text"}:
                    text = str(part.get("text") or "")
                    if remaining > 0:
                        chunks.append(text[:remaining])
                        remaining -= len(chunks[-1])
                elif part.get("type") in {"image_url", "input_image", "image"}:
                    image_count += 1
            if image_count:
                chunks.append(f"[{image_count} image attachment(s); binary payload omitted from history]")
            return "\n".join(chunk for chunk in chunks if chunk)
        return str(content or "")[:limit]

    @staticmethod
    def _intent_text(content) -> str:
        """Keep planning/routing focused on the user's request, not the full document body."""
        if isinstance(content, str):
            return content[:8_000]
        if isinstance(content, list):
            prompt = ""
            images = 0
            documents = 0
            for index, part in enumerate(content):
                if not isinstance(part, dict):
                    continue
                if part.get("type") in {"image_url", "input_image", "image"}:
                    images += 1
                elif part.get("type") in {"text", "input_text"}:
                    text = str(part.get("text") or "")
                    if index == 0:
                        prompt = text[:8_000]
                    elif text.startswith("--- ATTACHMENT:"):
                        documents += 1
            suffix = []
            if documents:
                suffix.append(f"{documents} document attachment(s)")
            if images:
                suffix.append(f"{images} image attachment(s)")
            return prompt + (("\n[" + ", ".join(suffix) + "]") if suffix else "")
        return str(content or "")[:8_000]

    @classmethod
    def _compact_messages(cls, messages: list[dict]) -> list[dict]:
        """Drop binary image payloads after a turn so history/context stays bounded."""
        compact = []
        for raw in messages:
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            if isinstance(item.get("content"), list):
                item["content"] = cls._content_text(item.get("content"))
            compact.append(item)
        return compact

    def _run_shared_runtime_impl(self, *, persistent_plan: bool):
        from ..agent_runtime import NativeLightRuntime

        state = get_state()
        runtime_label = "native-light" if persistent_plan else "classic"
        messages = list(self.messages)
        latest_user_index = next(
            (index for index in range(len(messages) - 1, -1, -1) if messages[index].get("role") == "user"),
            None,
        )
        if latest_user_index is None:
            self.error.emit(f"{runtime_label} runtime: no user message to execute")
            return
        initial_user_content = messages[latest_user_index].get("content")
        initial_prompt = self._intent_text(initial_user_content)
        prior = [
            dict(message) for message in messages[1:latest_user_index]
            if message.get("role") != "system"
        ]
        base_timeout = int(state.get("request_timeout", getattr(self.client, "timeout", 300)))

        def on_event(kind: str, payload: dict):
            if kind == "tools_ready":
                self.msg.emit(
                    "system",
                    f"{int(payload.get('count') or 0)} tools ready in {float(payload.get('elapsed') or 0.0):.2f}s",
                    runtime_label,
                )
            elif kind == "plan":
                plan = payload.get("plan")
                plan_id = getattr(plan, "id", "")
                action = str(payload.get("action") or "plan")
                if plan_id:
                    self.msg.emit("system", f"Persistent plan {action}: {plan_id}", runtime_label)
            elif kind == "model_response":
                requested = str(payload.get("requested") or "default")
                used = str(payload.get("model") or requested)
                route = used if used == requested else f"{requested} → {used}"
                self.msg.emit(
                    "system", f"Model response in {float(payload.get('elapsed_ms') or 0) / 1000.0:.1f}s", route
                )
            elif kind == "thought":
                self.msg.emit("thought", str(payload.get("text") or ""), f"step {payload.get('iteration', '?')}")
            elif kind == "tool_call":
                name = str(payload.get("name") or "?")
                args = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
                self.msg.emit("tool", f">> {name}({json.dumps(args, ensure_ascii=False)[:200]})", "")
            elif kind == "tool_result":
                name = str(payload.get("name") or "?")
                result = str(payload.get("result") or "")
                status = f"{'ERROR' if payload.get('is_error') else 'OK'} ({float(payload.get('elapsed') or 0.0):.1f}s)"
                self.msg.emit("tool_result", result[:2000], f"{name} {status}")
            elif kind == "model_switch":
                self.msg.emit(
                    "system", "Repeated tool loop detected; switching fallback model.",
                    f"{payload.get('previous', '?')} → {payload.get('model', '?')}",
                )

        resume_requested = persistent_plan and initial_prompt.strip().lower() in {
            "ja", "ja klar", "klar", "ok", "okay", "mach", "mache",
            "mach es", "weiter", "fortfahren", "yes", "sure", "go ahead", "continue",
        }
        runtime = NativeLightRuntime(
            client=self.client,
            initial_prompt=initial_prompt,
            initial_user_content=initial_user_content,
            model=self.model or None,
            fallback_model=self.fallback or None,
            workspace_root=str(active_workspace(state.get("workspace_root"))),
            tools=self.tools,
            system_prompt=self.system,
            conversation=prior,
            load_tools_on_start=self.load_tools_on_start,
            enabled_tool_names=self.enabled_tool_names,
            quick_chat=self.quick_chat and not resume_requested,
            approval_fn=self._gui_approval,
            event_fn=on_event,
            stop_requested=lambda: self._stopped,
            persistent_plan=persistent_plan,
            resume=resume_requested,
            base_timeout=base_timeout,
        )
        result = runtime.run()
        self.tools = result.tools
        self.system = result.system_prompt
        self.messages = self._compact_messages(result.messages)
        self.messages_updated.emit(self.messages)
        if result.status == "failed":
            self.error.emit(result.error or f"{runtime_label} runtime failed")
            return
        if result.status == "completed":
            self._emit_advisor_review(initial_prompt, result.response, result.model)
        self.finished.emit(result.response, result.model)

    def _run_impl(self):
        runtime_mode = get_state().get("runtime_mode", DEFAULT_RUNTIME_MODE)
        self._run_shared_runtime_impl(persistent_plan=(runtime_mode == "native-light"))


def _select_chat_route(model: str, fallback: str, quick_chat: bool):
    """Preserve the configured primary route; fallback means fallback."""
    return model, fallback, False


class PromptEdit(QPlainTextEdit):
    """Compact multiline prompt with direct clipboard-image paste support."""

    submitted = pyqtSignal()
    image_pasted = pyqtSignal(object)

    def insertFromMimeData(self, source):
        if source is not None and source.hasImage():
            self.image_pasted.emit(source.imageData())
            return
        super().insertFromMimeData(source)

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            modifiers = event.modifiers()
            if modifiers & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.AltModifier):
                super().keyPressEvent(event)
                return
            self.submitted.emit()
            event.accept()
            return
        super().keyPressEvent(event)


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
        self._attachments: list[Attachment] = []
        self._inflight_attachments: list[Attachment] = []
        self._activity_started = 0.0
        self._activity_label = ""
        self._activity_frame = 0
        self._activity_timer = QTimer(self)
        self._activity_timer.setInterval(120)
        self._activity_timer.timeout.connect(self._tick_activity)
        self._build_ui()
        self.setAcceptDrops(True)
        # Runtime configuration lives exclusively in the Settings tab/state store.
        # Chat reads persisted settings for every send, so Apply takes effect without restart.
        if self.settings_ref and hasattr(self.settings_ref, "tools_changed"):
            self.settings_ref.tools_changed.connect(self._on_tools_changed)

    def _on_tools_changed(self, _mode: str, _names):
        """Invalidate the filtered cache after a saved settings change."""
        self._tools = None
        self._system = None

    # ── Drag & Drop ──────────────────────────────────────────────
    def dragEnterEvent(self, event: QDragEnterEvent):
        mime = event.mimeData()
        if mime.hasUrls() or mime.hasImage() or mime.hasText():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        mime = event.mimeData()
        if mime.hasUrls():
            for url in mime.urls():
                path = url.toLocalFile()
                if path:
                    self._handle_dropped_file(path)
        elif mime.hasImage():
            self._handle_clipboard_image(mime.imageData())
        elif mime.hasText():
            text = mime.text().strip()
            if text:
                self.input.setPlainText(text)
        event.acceptProposedAction()

    def _update_attachment_ui(self):
        if not self._attachments:
            self.attachment_label.setText("No attachments · drag files here or paste a screenshot with Ctrl+V")
            self.clear_attachments_btn.setEnabled(False)
            return
        labels = []
        for item in self._attachments:
            suffix = "image" if item.kind == "image" else "document"
            labels.append(f"{item.name} ({suffix}, {item.size // 1024} KB)")
        self.attachment_label.setText(" · ".join(labels))
        self.clear_attachments_btn.setEnabled(True)

    def _add_attachment(self, item: Attachment):
        if len(self._attachments) >= MAX_ATTACHMENTS:
            raise ValueError(f"too many attachments (max {MAX_ATTACHMENTS})")
        total = sum(existing.size for existing in self._attachments) + item.size
        if total > MAX_TOTAL_ATTACHMENT_BYTES:
            raise ValueError(
                f"attachments exceed {MAX_TOTAL_ATTACHMENT_BYTES // (1024 * 1024)} MB total"
            )
        text_total = sum(len(existing.text) for existing in self._attachments) + len(item.text)
        if text_total > MAX_TOTAL_TEXT_CHARS:
            raise ValueError(f"document text exceeds {MAX_TOTAL_TEXT_CHARS:,} characters total")
        self._attachments.append(item)
        self._update_attachment_ui()
        note = f" · {item.note}" if item.note else ""
        self._append_msg("system", f"Attached: {item.name}", f"{item.kind}{note}")

    def _handle_dropped_file(self, path: str):
        try:
            self._add_attachment(load_path(path))
        except Exception as exc:
            self._append_msg("error", f"Attachment rejected: {Path(path).name}: {exc}")

    def _handle_clipboard_image(self, image):
        try:
            if image is None or getattr(image, "isNull", lambda: True)():
                raise ValueError("clipboard does not contain a usable image")
            buffer = QBuffer()
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            if not image.save(buffer, "PNG"):
                raise ValueError("could not encode clipboard image")
            raw = bytes(buffer.data())
            buffer.close()
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            self._add_attachment(image_attachment(f"clipboard-{stamp}.png", raw, "image/png"))
        except Exception as exc:
            self._append_msg("error", f"Clipboard image rejected: {exc}")

    def _choose_attachments(self):
        patterns = " ".join(f"*{suffix}" for suffix in sorted(SUPPORTED_SUFFIXES))
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Attach files", "", f"Supported files ({patterns});;All files (*)"
        )
        for path in paths:
            self._handle_dropped_file(path)

    def _clear_attachments(self):
        self._attachments.clear()
        self._update_attachment_ui()

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
        self.log.setObjectName("ChatLog")
        self.log.setReadOnly(True)
        # Let the parent ChatWidget own file/image drag-and-drop everywhere.
        self.log.setAcceptDrops(False)
        layout.addWidget(self.log, stretch=1)

        # Status-Zeile (erweitert: User, Tier, Workspace, Tools)
        self.status = QLabel("Ready.")
        self.status.setObjectName("Caption")
        layout.addWidget(self.status)

        # Attachments: documents, source files and screenshots.
        attachment_row = QHBoxLayout()
        self.attach_btn = QPushButton("Attach")
        self.attach_btn.setToolTip("Attach documents/source/images")
        self.attach_btn.clicked.connect(self._choose_attachments)
        attachment_row.addWidget(self.attach_btn)
        self.attachment_label = QLabel("")
        self.attachment_label.setObjectName("Caption")
        self.attachment_label.setWordWrap(True)
        attachment_row.addWidget(self.attachment_label, stretch=1)
        self.clear_attachments_btn = QPushButton("Clear")
        self.clear_attachments_btn.clicked.connect(self._clear_attachments)
        attachment_row.addWidget(self.clear_attachments_btn)
        layout.addLayout(attachment_row)
        self._update_attachment_ui()

        # Input-Zeile
        input_row = QHBoxLayout()
        self.input = PromptEdit()
        # Disabling drag/drop here does not affect clipboard paste; it lets file drops
        # over the editor bubble to ChatWidget while Ctrl+V still hits insertFromMimeData.
        self.input.setAcceptDrops(False)
        self.input.setPlaceholderText("Ask ai-coder…  Ctrl+V pastes screenshots · Enter sends")
        self.input.setFixedHeight(68)
        self.input.submitted.connect(self._send)
        self.input.image_pasted.connect(self._handle_clipboard_image)
        input_row.addWidget(self.input, stretch=1)

        self.send_btn = QPushButton("Send")
        self.send_btn.setObjectName("PrimaryButton")
        self.send_btn.setMinimumHeight(40)
        self.send_btn.clicked.connect(self._send)
        input_row.addWidget(self.send_btn)

        # Stop-Button — bricht laufenden Agent-Loop ab
        self.stop_btn = QPushButton("■")
        self.stop_btn.setObjectName("DangerButton")
        self.stop_btn.setToolTip("Stop agent (Esc)")
        self.stop_btn.setFixedWidth(38)
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop_agent)
        input_row.addWidget(self.stop_btn)

        # Clear-Button
        self.clear_btn = QPushButton("↺")
        self.clear_btn.setToolTip("New chat and reset context (Ctrl+Shift+L)")
        self.clear_btn.setFixedWidth(38)
        self.clear_btn.clicked.connect(self._clear_chat)
        input_row.addWidget(self.clear_btn)

        # History-Button
        self.history_btn = QPushButton("📋")
        self.history_btn.setToolTip("Chat history (Ctrl+H)")
        self.history_btn.setFixedWidth(38)
        self.history_btn.clicked.connect(self._show_history_menu)
        input_row.addWidget(self.history_btn)

        layout.addLayout(input_row)

        self._shortcuts = [
            QShortcut(QKeySequence("Ctrl+K"), self, activated=self.input.setFocus),
            QShortcut(QKeySequence("Escape"), self, activated=self._stop_agent),
            QShortcut(QKeySequence("Ctrl+Shift+L"), self, activated=self._clear_chat),
            QShortcut(QKeySequence("Ctrl+H"), self, activated=self._show_history_menu),
        ]

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
        ws = active_workspace(state.get("workspace_root"))
        parts.append(ws.name or str(ws))
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

    def _start_activity(self, model: str, tools_enabled: bool):
        self._activity_started = time.monotonic()
        self._activity_frame = 0
        route = model or "backend default"
        self._activity_label = f"{route} · {'agent tools' if tools_enabled else 'chat'}"
        self._activity_timer.start()
        self._tick_activity()

    def _tick_activity(self):
        frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        elapsed = max(0.0, time.monotonic() - self._activity_started)
        glyph = frames[self._activity_frame % len(frames)]
        self._activity_frame += 1
        self.status.setText(f"{glyph} Working · {elapsed:4.1f}s · {self._activity_label}")
        self.status.setStyleSheet("color: #43d9c0; font-size: 11px; font-weight: 600;")

    def _stop_activity(self):
        self._activity_timer.stop()

    def _clear_chat(self):
        self.log.clear()
        self._tools = None
        self._system = None
        self._messages = []
        self._attachments.clear()
        self._inflight_attachments.clear()
        self._update_attachment_ui()
        self._update_status_idle("Reset")
        self._append_msg("system", "Chat and context reset.", "")

    def _stop_agent(self):
        if self._worker and self._worker.isRunning():
            self._worker.stop()
            self._activity_label = "stopping safely"
            self._append_msg("system", "Stop requested...", "")

    @staticmethod
    def _approval_preview(arguments: dict, limit: int = 1200) -> str:
        """Render structured tool arguments without exposing common secrets."""
        sensitive = {"token", "password", "secret", "api_key", "apikey", "authorization"}

        def scrub(value):
            if isinstance(value, dict):
                return {
                    str(key): ("<redacted>" if str(key).lower() in sensitive else scrub(item))
                    for key, item in value.items()
                    if not str(key).startswith("_")
                }
            if isinstance(value, list):
                return [scrub(item) for item in value]
            return value

        try:
            rendered = json.dumps(scrub(arguments), ensure_ascii=False, indent=2, default=str)
        except Exception:
            rendered = repr(arguments)
        return rendered if len(rendered) <= limit else rendered[:limit] + "…"

    def _on_approval_needed(self, tool_name: str, arguments: object):
        """Apply the persisted approval policy and authenticate sudo locally."""
        args = dict(arguments) if isinstance(arguments, dict) else {}
        command = str(args.get("command", "") or "")
        risk = assess_execution(tool_name, args, destructive=is_destructive(command))
        preview = self._approval_preview(args)
        mode = get_state().get("approval_mode", "ask")
        scope_target = str(args.get("_workspace_escape") or "")
        scope_root = str(args.get("_workspace_root") or "")
        automatic = approval_is_automatic(mode, risk) and not scope_target

        if risk.elevation:
            QMessageBox.warning(
                self, "Root request rejected",
                "Das Coding-only-Profil führt keine Root-/sudo-Aktionen aus.\n\n"
                + format_request(risk),
            )
            if self._worker:
                self._worker.set_approval(False)
            return

        if not automatic:
            if scope_target:
                title = "Leave active workspace — allow once?"
            elif risk.deletion or risk.destructive:
                title = "Destructive operation — allow once?"
            elif risk.elevation:
                title = "Root operation — authenticate and allow once?"
            elif risk.mutation:
                title = "State-changing operation — allow once?"
            else:
                title = "Tool operation — allow once?"
            scope = (
                f"\n\nWorkspace boundary:\n  root: {scope_root}\n  target: {scope_target}"
                if scope_target else ""
            )
            msg = format_request(risk) + scope + "\n\nTool arguments:\n" + preview + "\n\nEinmal ausführen?"
            reply = QMessageBox.question(
                self, title, msg,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                if self._worker:
                    self._worker.set_approval(False)
                self._append_msg("system", f"Tool rejected: {tool_name}", preview[:160])
                return

        strategy = ""
        if risk.elevation:
            available, why = PrivilegeBroker.gui_elevation_available()
            if not available:
                if self._worker:
                    self._worker.set_approval(False)
                self._append_msg("system", f"Elevation blocked: {tool_name}", why)
                return
            strategy = "pkexec"

        if automatic:
            self._append_msg("system", f"Auto-approved: {tool_name}", f"mode={mode}")
        if self._worker:
            self._worker.set_approval(True, strategy)

    def _send(self):
        text = self.input.toPlainText().strip()
        if not text and not self._attachments:
            return
        if not text:
            text = "Analyze the attached file(s) and explain the relevant findings."
        attachment_names = ", ".join(item.name for item in self._attachments)
        self._append_msg("user", text, attachment_names)
        self.input.clear()
        self.send_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

        try:
            session = load_session()
            state = get_state()
            timeout = int(state.get("request_timeout", 30))
            client = TriForceClient(session.base_url, token=session.token, timeout=timeout)
        except Exception as e:
            self._append_msg("error", f"Keine Session: {e}")
            self.send_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            self._stop_activity()
            return

        # Model routing is configured in Settings and read fresh for every send.
        state = get_state()
        model = str(state.get("selected_model") or "")
        fallback = str(state.get("fallback_model") or "")

        resume_requested = (
            state.get("runtime_mode", DEFAULT_RUNTIME_MODE) == "native-light"
            and text.strip().lower() in {
                "ja", "ja klar", "klar", "ok", "okay", "mach", "mache",
                "mach es", "weiter", "fortfahren", "yes", "sure", "go ahead", "continue",
            }
        )
        quick_chat = is_simple_chat_message(text) and not resume_requested
        model, fallback, fast_fallback = _select_chat_route(model, fallback, quick_chat)
        if fast_fallback:
            self._append_msg("system", f"Fast chat model · {model}", "")
        tool_mode = state.get("tool_mode", "on_demand")
        enabled_tool_names = state.get("enabled_tools")
        should_load_tools_now = should_load_tools(
            tool_mode, text, resume=resume_requested
        ) and enabled_tool_names != []
        self._start_activity(model, should_load_tools_now)

        if should_load_tools_now and self._tools is None:
            self._append_msg("system", f"Loading selected tools · mode={tool_mode}", "")
        elif not should_load_tools_now:
            self._append_msg("system", "On demand · tools skipped for this prompt", "")

        run_tools = self._tools if should_load_tools_now else []
        run_system = self._system if should_load_tools_now and self._system else build_system_prompt(
            [], str(active_workspace(state.get("workspace_root")))
        )

        if not self._messages:
            self._messages = [{"role": "system", "content": run_system}]

        # Attachments are one-shot for the current user turn. Text documents become
        # bounded text blocks; images stay binary until this request reaches a vision model.
        turn_attachments = list(self._attachments)
        self._inflight_attachments = turn_attachments
        user_content = multimodal_content(text, turn_attachments)
        self._attachments.clear()
        self._update_attachment_ui()
        self._messages.append({"role": "user", "content": user_content})

        # Save to history
        if not self._session_id:
            title = text[:50].strip() or "New Chat"
            self._session_id = chat_history.create_session(title=title)
        chat_history.save_message(self._session_id, "user", text)

        self._worker = _AgentWorker(
            client, self._messages, model, fallback, run_tools, run_system,
            load_tools_on_start=should_load_tools_now,
            enabled_tool_names=enabled_tool_names,
            quick_chat=quick_chat,
        )
        self._worker.msg.connect(self._on_agent_msg)
        self._worker.finished.connect(self._on_response)
        self._worker.messages_updated.connect(self._on_messages_updated)
        self._worker.error.connect(self._on_error)
        self._worker.approval_needed.connect(self._on_approval_needed)
        self._worker.start()

    def _on_agent_msg(self, role: str, text: str, meta: str):
        self._append_msg(role, text, meta)

    def _on_response(self, text: str, model_used: str):
        self._stop_activity()
        self._inflight_attachments.clear()
        # Cache tools loaded by worker for next message
        if self._worker and self._worker.load_tools_on_start and self._worker.tools is not None:
            self._tools = self._worker.tools
            self._system = self._worker.system
        self._append_msg("assistant", text, model_used)
        if self._session_id:
            chat_history.save_message(self._session_id, "assistant", text, model_used)
        self.send_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._update_status_idle(f"Done ({model_used})")

    def _on_messages_updated(self, messages: list):
        self._messages = messages

    def _on_error(self, err: str):
        self._stop_activity()
        if self._inflight_attachments:
            current_names = {item.name for item in self._attachments}
            for item in self._inflight_attachments:
                if item.name not in current_names:
                    self._attachments.append(item)
            self._inflight_attachments.clear()
            self._update_attachment_ui()
            self._append_msg("system", "Attachments restored after failed request.", "retry available")
        self._append_msg("error", err)
        self.send_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._update_status_idle("Error")
