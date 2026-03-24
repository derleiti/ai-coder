"""Chat-Widget mit vollem Agent-Loop (MCP-Tools + local_exec)."""
from __future__ import annotations
import html
import json
import re
import time
from datetime import datetime
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QTextEdit, QLineEdit, QPushButton, QLabel,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QTextCursor

from ..config import load_session
from ..session_state import get_state
import platform
from ..client import TriForceClient, ClientError

IS_WINDOWS = platform.system() == 'Windows'

TOOL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL | re.IGNORECASE)


class _AgentWorker(QThread):
    """Background-Thread: voller Agent-Loop mit MCP-Tools."""
    msg = pyqtSignal(str, str, str)       # (role, text, meta)
    finished = pyqtSignal(str, str)        # (final_text, model)
    error = pyqtSignal(str)

    MAX_ITER = 12

    def __init__(self, client, message, model, fallback, tools, system_prompt):
        super().__init__()
        self.client = client
        self.message = message
        self.model = model
        self.fallback = fallback
        self.tools = tools
        self.system = system_prompt

    def run(self):
        history = []
        current_input = self.message
        model_used = self.model or "default"

        for i in range(self.MAX_ITER):
            # Kontext aufbauen
            if history:
                ctx = "\n\n".join(
                    f"User: {h['user']}\nAssistant: {h['assistant'][:600]}"
                    for h in history[-3:]
                )
                msg = ctx + f"\n\nUser: {current_input}"
            else:
                msg = current_input

            try:
                result = self.client.chat(
                    message=msg,
                    model=self.model or None,
                    fallback_model=self.fallback or None,
                    system_prompt=self.system,
                    temperature=0.3,
                    max_tokens=4096,
                )
            except (ClientError, Exception) as e:
                self.error.emit(str(e))
                return

            response = result.get("response", "").strip()
            model_used = result.get("model", self.model or "default")

            # Tool-Calls parsen
            calls = []
            for m in TOOL_RE.finditer(response):
                try:
                    c = json.loads(m.group(1).strip())
                    if "name" in c:
                        calls.append(c)
                except Exception:
                    pass

            visible = TOOL_RE.sub("", response).strip()

            if not calls:
                # Finale Antwort — kein Tool-Call
                self.finished.emit(response, model_used)
                return

            # Gedanken anzeigen
            if visible:
                self.msg.emit("thought", visible, f"step {i+1}")

            # Tools ausfuehren
            tool_results = []
            for call in calls:
                tname = call.get("name", "?")
                targs = call.get("arguments", {})
                self.msg.emit("tool", f">> {tname}({json.dumps(targs, ensure_ascii=False)[:200]})", "")

                t0 = time.time()
                tr, is_err = self._run_tool(tname, targs)
                elapsed = time.time() - t0

                status = f"{'ERROR' if is_err else 'OK'} ({elapsed:.1f}s)"
                self.msg.emit("tool_result", tr[:2000], f"{tname} {status}")
                tool_results.append(f"Tool {tname} result:\n{tr}")

            history.append({"user": current_input, "assistant": response})
            current_input = "\n\n".join(tool_results)

            if "DONE:" in response[:200].upper():
                self.finished.emit(visible or response, model_used)
                return

        self.finished.emit(f"(Max {self.MAX_ITER} Iterationen erreicht)\n{visible or response}", model_used)

    def _run_tool(self, name: str, args: dict) -> tuple[str, bool]:
        # local_exec: lokal via subprocess (OS-aware)
        if name == "local_exec":
            import subprocess as _sp
            cmd = args.get("command", "")
            cwd = args.get("cwd") or None
            if IS_WINDOWS:
                run_args = ["powershell", "-NoProfile", "-Command", cmd]
                try:
                    r = _sp.run(run_args, cwd=cwd, capture_output=True, text=True, timeout=60)
                    out = (r.stdout or "") + (r.stderr or "")
                    return (out[:4000] or "(no output)"), r.returncode != 0
                except Exception as e:
                    return f"local_exec error: {e}", True
            else:
                use_sudo = args.get("sudo", False)
                if use_sudo and not cmd.strip().startswith("sudo "):
                    cmd = "sudo " + cmd
                try:
                    r = _sp.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=60)
                    out = (r.stdout or "") + (r.stderr or "")
                    return (out[:4000] or "(no output)"), r.returncode != 0
                except Exception as e:
                    return f"local_exec error: {e}", True

        # MCP-Tools: Backend
        try:
            r = self.client.mcp_call(name, args)
            text = r.get("result", {}).get("content", [{}])[0].get("text", "")
            is_error = r.get("result", {}).get("isError", False)
            return text[:4000], is_error
        except ClientError as e:
            return f"TOOL FAILED: {e}", True


def _load_tools_and_system(client: TriForceClient) -> tuple[list, str]:
    """Laedt MCP-Tools und baut System-Prompt (wie agent.py)."""
    from ..agent import AGENT_TOOLS, LOCAL_EXEC_SCHEMA, SYSTEM, _FALLBACK_TOOLS

    # Tools laden
    mcp_tools = []
    try:
        short_client = TriForceClient(client.base_url, token=client.token, timeout=8)
        r = short_client._request("POST", "/v1/mcp",
            {"jsonrpc": "2.0", "method": "tools/list", "params": {}, "id": 1},
            require_auth=True, _label="tools/list")
        mcp_tools = [t for t in r.get("result", {}).get("tools", []) if t["name"] in AGENT_TOOLS]
    except Exception:
        pass
    if not mcp_tools:
        mcp_tools = _FALLBACK_TOOLS

    tools = [LOCAL_EXEC_SCHEMA] + mcp_tools

    # Tool-Beschreibungen
    lines = []
    for t in sorted(tools, key=lambda x: x["name"]):
        props = list(t.get("inputSchema", {}).get("properties", {}).keys())
        req = t.get("inputSchema", {}).get("required", [])
        sig = ", ".join(f"{p}*" if p in req else p for p in props)
        desc = (t.get("description", "") or "")[:100].replace("\n", " ")
        lines.append(f"- {t['name']}({sig}): {desc}")
    tool_str = "\n".join(lines)[:4000]

    # System-Prompt
    import subprocess, os
    from pathlib import Path
    from ..session_state import get_state
    state = get_state()
    ws_path = Path(state.get("workspace_root") or ".").resolve()
    try:
        entries = sorted(
            e.name for e in ws_path.iterdir()
            if e.name not in {".git", ".venv", "__pycache__", "node_modules"}
        )[:20]
        ws_str = f"path: {ws_path}\nfiles: {', '.join(entries)}"
    except Exception:
        ws_str = f"path: {ws_path}"

    from ..agent import OS_NAME, OS_INSTRUCTIONS
    system = SYSTEM.format(agents_md="", tools=tool_str, workspace=ws_str[:300], os_name=OS_NAME, os_instructions=OS_INSTRUCTIONS)
    return tools, system


class ChatWidget(QWidget):
    def __init__(self, settings_ref=None, parent=None):
        super().__init__(parent)
        self.settings_ref = settings_ref
        self._worker = None
        self._tools = None
        self._system = None
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
                background: #111; color: #fff;
                border: 1px solid #444; border-radius: 4px;
                padding: 8px 12px; font-size: 13px;
            }
            QLineEdit:focus { border-color: #00d4ff; }
        """)
        self.input.returnPressed.connect(self._send)
        input_row.addWidget(self.input, stretch=1)

        self.send_btn = QPushButton("Senden")
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

        layout.addLayout(input_row)

    def _append_msg(self, role: str, text: str, meta: str = ""):
        ts = datetime.now().strftime("%H:%M:%S")
        esc = html.escape(text)
        colors = {
            "user": ("#00d4ff", "Du"),
            "assistant": ("#00ff88", "AI"),
            "thought": ("#888", "Gedanke"),
            "tool": ("#ff9800", "Tool"),
            "tool_result": ("#aaa", "Ergebnis"),
            "error": ("#ff6b6b", "Fehler"),
            "system": ("#666", "System"),
        }
        color, label = colors.get(role, ("#ccc", role))
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
        self.status.setText("Agent arbeitet...")
        self.status.setStyleSheet("color: #00d4ff; font-size: 11px;")

        # Client aus Session
        try:
            session = load_session()
            client = TriForceClient(session.base_url, token=session.token, timeout=120)
        except Exception as e:
            self._append_msg("error", f"Keine Session: {e}")
            self.send_btn.setEnabled(True)
            self.status.setText("Nicht eingeloggt.")
            self.status.setStyleSheet("color: #ff6b6b; font-size: 11px;")
            return

        # Tools + System-Prompt laden (einmalig oder wenn noch nicht geladen)
        if self._tools is None:
            self._append_msg("system", "Lade MCP-Tools...", "")
            try:
                self._tools, self._system = _load_tools_and_system(client)
                self._append_msg("system", f"{len(self._tools)} Tools geladen", "")
            except Exception as e:
                self._append_msg("error", f"Tool-Loading: {e}")
                self._tools = []
                # Fallback system prompt
                self._system = (
                    "Du bist ai-coder, ein Coding- und DevOps-Assistent von AILinux. "
                    "Antworte praezise. Sprache: Deutsch."
                )

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

        self._worker = _AgentWorker(client, text, model, fallback, self._tools, self._system)
        self._worker.msg.connect(self._on_agent_msg)
        self._worker.finished.connect(self._on_response)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _on_agent_msg(self, role: str, text: str, meta: str):
        self._append_msg(role, text, meta)

    def _on_response(self, text: str, model_used: str):
        self._append_msg("assistant", text, model_used)
        self.send_btn.setEnabled(True)
        self.status.setText(f"Fertig ({model_used})")
        self.status.setStyleSheet("color: #00ff88; font-size: 11px;")

    def _on_error(self, err: str):
        self._append_msg("error", err)
        self.send_btn.setEnabled(True)
        self.status.setText("Fehler.")
        self.status.setStyleSheet("color: #ff6b6b; font-size: 11px;")
