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
    QTextEdit, QPlainTextEdit, QPushButton, QLabel, QMessageBox,
    QMenu, QInputDialog,
)
from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal, QMetaObject, Q_ARG, QMimeData, QUrl
from PyQt6.QtGui import (
    QTextCursor, QDragEnterEvent, QDropEvent, QKeyEvent, QKeySequence, QShortcut,
)

from ..config import load_session
from ..privileges import (
    PrivilegeBroker, assess_execution, format_request,
)
from ..session_state import DEFAULT_RUNTIME_MODE, get_state
from ..workspace import active_workspace, workspace_from_task
from ..workspace_backend import open_workspace_for_run, preserve_workspace_for_resume
from ..client import TriForceClient
from .. import chat_history
from ..executor import (
    build_system_prompt, is_destructive, is_short_confirmation, is_simple_chat_message, should_load_tools,
)


class _AgentWorker(QThread):
    """Background thread: agent loop with approval support and stop."""
    msg = pyqtSignal(str, str, str)          # (role, text, meta)
    activity = pyqtSignal(str)
    finished = pyqtSignal(str, str)           # (final_text, model)
    error = pyqtSignal(str)
    messages_updated = pyqtSignal(list)
    approval_needed = pyqtSignal(str, object)  # tool, complete approval arguments

    def __init__(
        self, client, messages_array, model, tools, system_prompt,
        load_tools_on_start=True, enabled_tool_names=None, quick_chat=False,
        tools_unavailable_reason="", progressive_tool_disclosure=True,
        session_id="", evidence_context="",
    ):
        super().__init__()
        self.client = client
        self.messages = list(messages_array)
        self.model = model
        self.tools = tools
        self.system = system_prompt
        self.load_tools_on_start = load_tools_on_start
        self.enabled_tool_names = enabled_tool_names
        self.quick_chat = quick_chat
        self.tools_unavailable_reason = str(tools_unavailable_reason or "")
        self.progressive_tool_disclosure = bool(progressive_tool_disclosure)
        self.session_id = str(session_id or "")
        self.evidence_context = str(evidence_context or "")
        # Approval mechanism: threading.Event + result flag
        self._approval_event = threading.Event()
        self._approval_result = False
        self._approval_strategy = ""
        self._stopped = False

    def stop(self):
        """Request stop from main thread."""
        self._stopped = True

    def set_approval(self, approved: bool, strategy: str = ""):
        """Called from main thread after user decision."""
        self._approval_result = approved
        self._approval_strategy = str(strategy or "")
        self._approval_event.set()

    def _gui_approval(self, tool_name: str, args: dict) -> bool:
        """Approval callback for risky workspace writes."""
        approval_args = dict(args)
        # Preserve the complete, security-enriched argument map. Structured MCP
        # writes often have no command string; dropping metadata here made the
        # GUI dialog misclassify or hide the requested operation.
        self._approval_event.clear()
        self._approval_result = False
        self._approval_strategy = ""
        self.approval_needed.emit(tool_name, approval_args)
        # Poll every 2s so stop() is respected during pending approval
        for _ in range(60):  # max 120s total
            if self._approval_event.wait(timeout=2):
                break
            if self._stopped:
                return False  # Agent stopped — reject command
        if self._approval_result and self._approval_strategy:
            args["_elevation_strategy"] = self._approval_strategy
        return self._approval_result

    def run(self):
        # A QThread exception must never terminate the GUI process.
        try:
            self._run_impl()
        except Exception as exc:
            import traceback
            try:
                self.error.emit(f"Agent crashed: {exc}\n{traceback.format_exc()}")
            except Exception:
                pass

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
        initial_prompt = str(messages[latest_user_index].get("content") or "")
        prior = [
            dict(message) for message in messages[1:latest_user_index]
            if message.get("role") != "system"
        ]
        if self.evidence_context:
            prior.append({"role": "assistant", "content": self.evidence_context})
        base_timeout = int(state.get("request_timeout", getattr(self.client, "timeout", 300)))
        active_plan_id = ""

        def on_event(kind: str, payload: dict):
            nonlocal active_plan_id
            if kind == "tools_ready":
                self.msg.emit(
                    "system",
                    f"{int(payload.get('count') or 0)} tools ready in {float(payload.get('elapsed') or 0.0):.2f}s",
                    runtime_label,
                )
            elif kind == "run_start":
                configured = str(payload.get("configured_model") or payload.get("model") or "default")
                effective = str(payload.get("effective_model") or payload.get("model") or configured)
                route = configured if configured == effective else f"{configured} → {effective}"
                detail = f"effective={route}"
                detail += f" · transport={payload.get('transport') or 'default'}"
                self.msg.emit("system", "Run route", detail)
                if bool(payload.get("resumed")):
                    self.msg.emit(
                        "system",
                        "Resume safety",
                        "Raw tool evidence is not restored · fresh inspection required before mutation",
                    )
            elif kind == "performance_warning":
                warning_kind = str(payload.get("kind") or "performance")
                elapsed = int(payload.get("elapsed_ms") or 0) / 1000.0
                if warning_kind == "model_latency":
                    text = f"High model/API latency detected: {elapsed:.1f}s"
                elif warning_kind == "filesystem_latency":
                    text = f"Slow filesystem operation: {payload.get('tool') or 'I/O'} {elapsed:.1f}s"
                else:
                    text = str(payload.get("message") or "Performance bottleneck detected")
                self.msg.emit("system", text, "performance")
            elif kind == "performance_summary":
                wall = int(payload.get("wall_ms") or 0) / 1000.0
                model_s = int(payload.get("model_ms") or 0) / 1000.0
                tools_s = int(payload.get("tool_ms") or 0) / 1000.0
                io_s = int(payload.get("filesystem_ms") or 0) / 1000.0
                bottleneck = str(payload.get("bottleneck") or "?")
                self.msg.emit(
                    "system",
                    f"Performance: {wall:.1f}s wall · {model_s:.1f}s model · {tools_s:.1f}s tools · {io_s:.1f}s I/O",
                    f"bottleneck={bottleneck}",
                )
            elif kind == "model_without_tool_support":
                _msg = (
                    f"{payload.get('model') or '?'} meldet kein natives Function Calling — "
                    "AICoder verwendet weiterhin textbasiertes Tool-Calling; "
                    "native Provider-Toolschemas werden nicht gesendet."
                )
                self.msg.emit("system", _msg, runtime_label)
            elif kind == "plan":
                plan = payload.get("plan")
                plan_id = getattr(plan, "id", "")
                action = str(payload.get("action") or "plan")
                if plan_id:
                    active_plan_id = str(plan_id)
                    self.msg.emit("system", f"Persistent plan {action}: {plan_id}", runtime_label)
            elif kind == "model_start":
                phase = str(payload.get("phase") or "planning")
                model_name = str(payload.get("model") or self.model or "backend")
                self.activity.emit(f"Waiting for model · {model_name} · {phase} · idle timeout {int(payload.get('timeout') or 0)}s")
            elif kind == "model_response":
                requested = str(payload.get("requested") or "default")
                used = str(payload.get("model") or requested)
                route = used if used == requested else f"{requested} → {used}"
                telemetry = payload.get("transport_telemetry") if isinstance(payload.get("transport_telemetry"), dict) else {}
                meta = route
                request_id = str(payload.get("request_id") or telemetry.get("request_id") or "")
                if request_id:
                    meta += f" · req {request_id[-8:]}"
                if telemetry:
                    meta += (
                        f" · transport {float(telemetry.get('elapsed_s') or 0.0):.1f}s"
                        f" · {int(telemetry.get('chunks') or 0)} chunks"
                        f" · max gap {float(telemetry.get('max_rx_gap_s') or 0.0):.1f}s"
                    )
                self.msg.emit(
                    "system", f"Model response in {float(payload.get('elapsed_ms') or 0) / 1000.0:.1f}s", meta
                )
            elif kind == "thought":
                self.msg.emit("thought", str(payload.get("text") or ""), f"step {payload.get('iteration', '?')}")
            elif kind == "tool_call":
                name = str(payload.get("name") or "?")
                self.activity.emit(f"Running tool · {name}")
                args = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
                self.msg.emit("tool", f">> {name}({json.dumps(args, ensure_ascii=False)[:200]})", "")
            elif kind == "tool_result":
                name = str(payload.get("name") or "?")
                result = str(payload.get("result") or "")
                is_error = bool(payload.get("is_error"))
                evidence_status = "blocked" if is_error and "blocked" in result.lower() else ("error" if is_error else "ok")
                if self.session_id:
                    try:
                        chat_history.save_tool_event(
                            self.session_id, name, evidence_status,
                            iteration=int(payload.get("iteration") or 0),
                            plan_id=active_plan_id,
                        )
                    except Exception:
                        pass
                status = f"{'ERROR' if is_error else 'OK'} ({float(payload.get('elapsed') or 0.0):.1f}s)"
                self.activity.emit(f"Tool completed · {name} · {'ERROR' if is_error else 'OK'}")
                self.msg.emit("tool_result", result[:2000], f"{name} {status}")
            elif kind == "loop_prevented":
                self.activity.emit("Duplicate tool call blocked · waiting for corrected model action")
                self.msg.emit(
                    "system",
                    "Duplicate tool call blocked before execution; asking the model to continue from the existing result.",
                    f"repeat {int(payload.get('repeats') or 0)}",
                )
            elif kind == "model_switch":
                self.msg.emit(
                    "system", "Repeated tool loop detected; switching fallback model.",
                    f"{payload.get('previous', '?')} → {payload.get('model', '?')}",
                )

        resume_requested = persistent_plan and is_short_confirmation(initial_prompt)
        source_workspace = str(workspace_from_task(initial_prompt, state.get("workspace_root")))
        workspace_mode = str(state.get("workspace_mode", "auto") or "auto")
        workspace_backend = open_workspace_for_run(
            source_workspace, workspace_mode, resume=resume_requested, resume_plan_id=None,
        )
        info = workspace_backend.info
        workspace_detail = info.mode
        if info.mode == "ram":
            workspace_detail += f" · transactional · budget {info.safe_budget_bytes // (1024**2)} MiB"
            if info.restored_checkpoint:
                workspace_detail += " · restored"
        elif info.fallback_reason:
            workspace_detail += f" · fallback: {info.fallback_reason}"
        self.msg.emit("system", f"Execution workspace: {workspace_detail}", runtime_label)
        runtime = NativeLightRuntime(
            client=self.client,
            initial_prompt=initial_prompt,
            model=self.model or None,
            fallback_model=None,
            workspace_root=str(info.execution_root),
            plan_workspace_root=source_workspace,
            protected_workspace_root=(source_workspace if workspace_backend.info.transactional else None),
            completion_guard=(lambda: workspace_backend.finalize(verified=True)),
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
            max_output_tokens=int(state.get("max_output_tokens", 16384)),
            tools_unavailable_reason=self.tools_unavailable_reason,
            progressive_tool_disclosure=self.progressive_tool_disclosure,
            native_openrouter_tool_calling=bool(state.get("native_openrouter_tool_calling", False)),
        )
        result = runtime.run()
        if result.status != "completed":
            preserve_workspace_for_resume(workspace_backend, result.plan_id or None)
        self.tools = result.tools
        self.system = result.system_prompt
        self.messages = result.messages
        self.messages_updated.emit(result.messages)
        if result.status == "failed":
            self.error.emit(result.error or f"{runtime_label} runtime failed")
            return
        self.finished.emit(result.response, result.model)

    def _run_team_impl(self, initial_prompt: str):
        from ..model_transport import native_model_transport_from_env
        from ..team_orchestrator import run_team
        from ..team_runtime import config_from_state

        state = get_state()
        source_workspace = str(workspace_from_task(initial_prompt, state.get("workspace_root")))
        config = config_from_state(state)
        model_client, _ = native_model_transport_from_env(self.client, default_model=self.model or None)

        def on_team_event(kind: str, payload: dict):
            if kind == "team_start":
                self.activity.emit(f"Team runtime · {payload.get('agents', 0)} configured roles")
                self.msg.emit(
                    "system",
                    f"Team runtime started · {payload.get('research', 0)} research · {payload.get('coders', 0)} coders",
                    "RAM candidates",
                )
            elif kind == "team_pipeline":
                stage = str(payload.get("stage") or "stage")
                status = str(payload.get("status") or "?")
                self.activity.emit(f"Team · {stage} · {status}")
                if status == "started":
                    self.msg.emit("system", f"Pipeline · {stage}", "stage")
            elif kind == "team_workspace_plan":
                mode = str(payload.get("backend_mode") or "?")
                count = int(payload.get("candidate_count") or 0)
                reason = str(payload.get("reason") or "").strip()
                detail = f"{count} Kandidaten · {mode}" + (f" · {reason}" if reason else "")
                self.msg.emit("system", "Workspace-Plan", detail)
            elif kind == "team_stage":
                role = str(payload.get("role") or "stage")
                status = str(payload.get("status") or "?")
                model = str(payload.get("model") or "?")
                elapsed = int(payload.get("elapsed_ms") or 0) / 1000.0
                self.activity.emit(f"Team · {role} · {status}")
                self.msg.emit("system", f"{role} · {status} · {elapsed:.1f}s", model)
            elif kind == "team_candidate":
                cid = str(payload.get("candidate_id") or "candidate")
                self.msg.emit(
                    "system",
                    f"{cid} · {payload.get('status')} · score {payload.get('score')}",
                    "anonymized candidate",
                )
            elif kind == "team_complete":
                self.msg.emit(
                    "system",
                    f"Team complete · winner {payload.get('winner_candidate_id')} · score {payload.get('winner_score')}",
                    f"wall {int(payload.get('wall_ms') or 0)/1000.0:.1f}s",
                )

        result = run_team(
            task=initial_prompt, state=state, config=config, client=self.client,
            model_client=model_client, source_workspace=source_workspace,
            event_fn=on_team_event, stop_requested=lambda: self._stopped,
        )
        if result.status != "completed":
            self.error.emit(result.error or "Team runtime failed")
            return
        self.messages.append({"role": "assistant", "content": result.response})
        self.messages_updated.emit(self.messages)
        self.finished.emit(result.response, result.model)

    def _run_impl(self):
        state = get_state()
        messages = list(self.messages)
        latest = next((str(m.get("content") or "") for m in reversed(messages) if m.get("role") == "user"), "")
        from ..team_runtime import should_use_team
        if latest and should_use_team(latest, str(state.get("team_runtime_mode") or "off")) and not is_short_confirmation(latest):
            self._run_team_impl(latest)
            return
        runtime_mode = state.get("runtime_mode", DEFAULT_RUNTIME_MODE)
        self._run_shared_runtime_impl(persistent_plan=(runtime_mode == "native-light"))



class PromptEdit(QPlainTextEdit):
    """Compact multiline prompt: Enter sends, Alt/Shift+Enter adds a line."""

    submitted = pyqtSignal()

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
        self._dropped_files = []
        self._activity_started = 0.0
        self._activity_label = ""
        self._activity_frame = 0
        self._activity_timer = QTimer(self)
        self._activity_timer.setInterval(120)
        self._activity_timer.timeout.connect(self._tick_activity)
        self._build_ui()
        self.setAcceptDrops(True)
        if self.settings_ref and hasattr(self.settings_ref, "tools_changed"):
            self.settings_ref.tools_changed.connect(self._on_tools_changed)

    def _on_tools_changed(self, _mode: str, _names):
        """Invalidate the filtered cache after a settings change."""
        self._tools = None
        self._system = None

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
                self.input.setPlainText(text)
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
        self.log.setObjectName("ChatLog")
        self.log.setReadOnly(True)
        layout.addWidget(self.log, stretch=1)

        # Status-Zeile (erweitert: User, Tier, Workspace, Tools)
        self.status = QLabel("Ready.")
        self.status.setObjectName("Caption")
        layout.addWidget(self.status)

        # Input-Zeile
        input_row = QHBoxLayout()
        self.input = PromptEdit()
        self.input.setPlaceholderText("Ask ai-coder…  Enter sends · Shift/Alt+Enter adds a line")
        self.input.setFixedHeight(68)
        self.input.submitted.connect(self._send)
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

    def _on_worker_activity(self, label: str):
        self._activity_label = str(label or "working")
        if self._activity_timer.isActive():
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
        """Apply policy and reply to the worker that actually requested approval."""
        try:
            sender = self.sender()
        except RuntimeError:
            sender = None
        approval_worker = sender if isinstance(sender, _AgentWorker) else self._worker
        args = dict(arguments) if isinstance(arguments, dict) else {}
        command = str(args.get("command", "") or "")
        risk = assess_execution(tool_name, args, destructive=is_destructive(command))
        preview = self._approval_preview(args)
        mode = get_state().get("approval_mode", "ask")
        scope_target = str(args.get("_workspace_escape") or "")
        scope_root = str(args.get("_workspace_root") or "")
        decision = PrivilegeBroker.evaluate(mode, risk, workspace_escape=bool(scope_target), headless=False)
        automatic = decision.automatic

        if decision.requires_confirmation:
            if scope_target:
                title = "Leave active workspace — allow once?"
            elif risk.deletion or risk.destructive:
                title = "Destructive operation — allow once?"
            elif risk.elevation:
                title = "Root operation — authenticate and allow once?"
            elif risk.security_change:
                title = "Security setting change — allow once?"
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
                if approval_worker:
                    approval_worker.set_approval(False)
                self._append_msg("system", f"Tool rejected: {tool_name}", preview[:160])
                return

        strategy = ""
        if risk.elevation:
            available, why = PrivilegeBroker.gui_elevation_available()
            if not available:
                if approval_worker:
                    approval_worker.set_approval(False)
                self._append_msg("system", f"Elevation blocked: {tool_name}", why)
                return
            strategy = "pkexec"

        if automatic:
            self._append_msg("system", f"Auto-approved: {tool_name}", f"mode={mode}")
        if approval_worker:
            approval_worker.set_approval(True, strategy)

    def _send(self):
        if self._worker and self._worker.isRunning():
            self._append_msg("system", "Agent already running; duplicate send ignored.", "")
            return
        text = self.input.toPlainText().strip()
        if not text:
            return
        self._append_msg("user", text)
        self.input.clear()
        self.send_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

        try:
            session = load_session()
            state = get_state()
            timeout = int(state.get("request_timeout", 30))
            if self.settings_ref and hasattr(self.settings_ref, "get_request_timeout"):
                timeout = self.settings_ref.get_request_timeout()
            client = TriForceClient(session.base_url, token=session.token, timeout=timeout)
        except Exception as e:
            self._append_msg("error", f"Keine Session: {e}")
            self.send_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            self._stop_activity()
            return

        # Model routing is settings-driven. The Chat page never overrides it.
        state = get_state()
        model = str(state.get("selected_model") or "").strip()

        resume_requested = (
            state.get("runtime_mode", DEFAULT_RUNTIME_MODE) == "native-light"
            and is_short_confirmation(text)
        )
        quick_chat = is_simple_chat_message(text) and not resume_requested
        tool_mode = state.get("tool_mode", "on_demand")
        enabled_tool_names = state.get("enabled_tools")
        if self.settings_ref and hasattr(self.settings_ref, "get_tool_mode"):
            tool_mode = self.settings_ref.get_tool_mode()
            enabled_tool_names = self.settings_ref.get_enabled_tool_names()
        tools_requested = should_load_tools(tool_mode, text, resume=resume_requested)
        should_load_tools_now = tools_requested and enabled_tool_names != []
        tools_unavailable_reason = ""
        if tools_requested and enabled_tool_names == []:
            tools_unavailable_reason = (
                "No tools are enabled. Open Settings, load the available tools, and select "
                "the tools AICoder may use before running this task."
            )
        self._start_activity(model, should_load_tools_now)

        if should_load_tools_now and self._tools is None:
            self._append_msg("system", f"Loading selected tools · mode={tool_mode}", "")
        elif not should_load_tools_now:
            self._append_msg("system", "On demand · tools skipped for this prompt", "")

        if should_load_tools_now:
            # Rebuild the effective tool working set/system prompt for every run.
            # load_tools() already has a per-account TTL cache, so this is cheap,
            # while avoiding stale AGENTS.md, stale tool descriptions, or a system
            # prompt inherited from a previous task. on_demand will additionally
            # resolve a small capability working set inside NativeLightRuntime.
            run_tools = None
            run_system = None
        else:
            run_tools = []
            run_system = build_system_prompt([], str(active_workspace(state.get("workspace_root"))))

        if not self._messages:
            self._messages = [{"role": "system", "content": run_system}]
        elif self._messages[0].get("role") != "system":
            # History rows intentionally persist only visible chat messages. Runtime
            # conversation handling expects slot 0 to be the current system prompt.
            self._messages.insert(0, {"role": "system", "content": run_system})
        else:
            self._messages[0] = {"role": "system", "content": run_system}

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
        evidence_context = chat_history.render_tool_evidence(self._session_id, limit=40)

        self._worker = _AgentWorker(
            client, self._messages, model, run_tools, run_system,
            load_tools_on_start=should_load_tools_now,
            enabled_tool_names=enabled_tool_names,
            quick_chat=quick_chat,
            tools_unavailable_reason=tools_unavailable_reason,
            progressive_tool_disclosure=(tool_mode == "on_demand"),
            session_id=self._session_id, evidence_context=evidence_context,
        )
        self._worker.msg.connect(self._on_agent_msg)
        self._worker.finished.connect(self._on_response)
        self._worker.activity.connect(self._on_worker_activity)
        self._worker.messages_updated.connect(self._on_messages_updated)
        self._worker.error.connect(self._on_error)
        self._worker.approval_needed.connect(
            self._on_approval_needed, Qt.ConnectionType.QueuedConnection
        )
        self._worker.start()

    def _on_agent_msg(self, role: str, text: str, meta: str):
        self._append_msg(role, text, meta)

    def _on_response(self, text: str, model_used: str):
        self._stop_activity()
        # Cache tools loaded by worker for next message
        if (
            self._worker and self._worker.load_tools_on_start
            and not self._worker.progressive_tool_disclosure
            and self._worker.tools is not None
        ):
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
        self._append_msg("error", err)
        self.send_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._update_status_idle("Error")
