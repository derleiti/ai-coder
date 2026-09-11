"""Hauptfenster fuer ai-coder GUI — Tabs: Chat + Settings."""
from __future__ import annotations
from PyQt6.QtWidgets import (
    QMainWindow, QTabWidget, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
)
from PyQt6.QtCore import Qt, QSize, QObject, pyqtSignal
from PyQt6.QtGui import QKeySequence, QShortcut

from .chat_hub_widget import ChatHubWidget
from .settings_widget import SettingsWidget
from .mcp_widget import MCPServersWidget
from .shared_notify_widget import SharedNotifyWidget
from .theme import APP_STYLESHEET


class _SystemLogBridge(QObject):
    notification = pyqtSignal(object)
    event = pyqtSignal(str, object)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.tray = None  # wird von app.py gesetzt
        self.setWindowTitle("ai-coder")
        self.setMinimumSize(QSize(720, 540))
        self.resize(940, 720)

        self._apply_style()

        root = QWidget()
        root.setObjectName("AppRoot")
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(14, 12, 14, 14)
        root_layout.setSpacing(10)

        top_bar = QFrame()
        top_bar.setObjectName("TopBar")
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(14, 8, 14, 8)
        mark = QLabel(">_")
        mark.setObjectName("BrandMark")
        brand = QLabel("ai-coder")
        brand.setObjectName("Brand")
        caption = QLabel("AILinux operator agent")
        caption.setObjectName("Caption")
        shortcut_hint = QLabel("Ctrl+1 Chat   Ctrl+2 Settings   Ctrl+3 MCP   Ctrl+4 AI Network   Ctrl+K Prompt")
        shortcut_hint.setObjectName("Caption")
        top_layout.addWidget(mark)
        top_layout.addWidget(brand)
        top_layout.addWidget(caption)
        top_layout.addStretch()
        top_layout.addWidget(shortcut_hint)
        root_layout.addWidget(top_bar)

        self.tabs = QTabWidget()
        self.settings_tab = SettingsWidget()
        self.chat_tab = ChatHubWidget(settings_ref=self.settings_tab)
        self.mcp_tab = MCPServersWidget()
        self.network_tab = SharedNotifyWidget()

        self.tabs.addTab(self.chat_tab, "Chat")
        self.tabs.addTab(self.settings_tab, "Settings")
        self.tabs.addTab(self.mcp_tab, "MCP Servers")
        self.tabs.addTab(self.network_tab, "AI Network")
        self.network_tab.conversation_open_requested.connect(self._open_notify_conversation)
        root_layout.addWidget(self.tabs, stretch=1)
        self.setCentralWidget(root)

        self._shortcuts = [
            QShortcut(QKeySequence("Ctrl+1"), self, activated=lambda: self.tabs.setCurrentIndex(0)),
            QShortcut(QKeySequence("Ctrl+2"), self, activated=lambda: self.tabs.setCurrentIndex(1)),
            QShortcut(QKeySequence("Ctrl+,"), self, activated=lambda: self.tabs.setCurrentIndex(1)),
            QShortcut(QKeySequence("Ctrl+3"), self, activated=lambda: self.tabs.setCurrentIndex(2)),
            QShortcut(QKeySequence("Ctrl+4"), self, activated=lambda: self.tabs.setCurrentIndex(3)),
        ]
        self._setup_system_log_monitor()


    def _open_notify_conversation(self, conversation):
        self.chat_tab.open_conversation(conversation)
        self.tabs.setCurrentWidget(self.chat_tab)

    def _setup_system_log_monitor(self):
        from ..session_state import get_state
        from ..system_log_monitor import JournalctlSource, SystemLogMonitor, config_from_state, current_model_analyzer
        self._system_log_bridge = _SystemLogBridge(self)
        self._system_log_bridge.notification.connect(self._show_system_log_notification)
        self._system_log_bridge.event.connect(self._show_system_log_event)
        config = config_from_state(get_state())
        self._system_log_monitor = SystemLogMonitor(
            JournalctlSource(), current_model_analyzer(), config=config,
            notify=self._system_log_bridge.notification.emit,
            event_sink=self._system_log_bridge.event.emit,
        )
        self.settings_tab.systemlog_analyze_requested.connect(self._analyze_system_logs_now)
        self._system_log_monitor.start()

    def _analyze_system_logs_now(self):
        import threading
        threading.Thread(target=self._run_manual_system_log_analysis, name="aicoder-systemlog-manual", daemon=True).start()

    def _run_manual_system_log_analysis(self):
        try:
            analyses = self._system_log_monitor.analyze_now()
            if not analyses:
                self._system_log_bridge.event.emit("system_log_manual_empty", {})
                return
            for analysis in analyses:
                self._system_log_bridge.notification.emit(analysis)
        except Exception as exc:
            self._system_log_bridge.event.emit("system_log_monitor_error", {"error": str(exc)})

    def _show_system_log_event(self, name, payload):
        if name == "system_log_monitor_error":
            self.chat_tab._append_msg("error", "System log monitor", str(payload.get("error") or "unknown error"))
        elif name == "system_log_manual_empty":
            self.chat_tab._append_msg("system", "Systemlog analysis: no suspicious events found in the configured time window.", "read-only")

    def _show_system_log_notification(self, analysis):
        text = f"{analysis.title}\n{analysis.summary}\n\nReason: {analysis.reason}\nSuggested: {analysis.recommended_action}"
        self.chat_tab._append_msg("system", text, f"{analysis.severity} · {analysis.source} · confidence {analysis.confidence:.0%}")
        if self.tray and self.tray.isVisible():
            self.tray.showMessage(f"AICoder · {analysis.severity.upper()}", f"{analysis.title}: {analysis.summary}"[:500], self.tray.MessageIcon.Warning, 8000)

    def _apply_style(self):
        self.setStyleSheet(APP_STYLESHEET)

    def closeEvent(self, event):
        """Minimize to tray statt schliessen."""
        if self.tray and self.tray.isVisible():
            self.hide()
            self.tray.showMessage(
                "ai-coder",
                "Minimiert in die Taskleiste. Klick zum Oeffnen.",
                self.tray.MessageIcon.Information,
                2000,
            )
            event.ignore()
        else:
            if hasattr(self, "_system_log_monitor"):
                self._system_log_monitor.stop()
            event.accept()

    def show_and_raise(self):
        self.showNormal()
        self.activateWindow()
        self.raise_()
