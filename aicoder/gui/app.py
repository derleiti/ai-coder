"""QApplication + System Tray mit erweitertem Menue."""
from __future__ import annotations
import sys
import platform
import os

# Windows: Console-Fenster verstecken wenn GUI startet
if platform.system() == "Windows":
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass

from PyQt6.QtWidgets import QApplication, QSystemTrayIcon, QMenu
from PyQt6.QtGui import QIcon, QPixmap, QPainter, QColor, QAction
from PyQt6.QtCore import Qt

from .autostart import is_autostart_enabled, toggle_autostart
from ..helper_control import helper_status, open_helper, start_helper, stop_managed_helper


def _make_icon() -> QIcon:
    px = QPixmap(64, 64)
    px.fill(QColor(0, 0, 0, 0))
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#00d4ff"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(4, 4, 56, 56, 12, 12)
    p.setPen(QColor("#0a0a1a"))
    f = p.font()
    f.setPixelSize(28)
    f.setBold(True)
    p.setFont(f)
    p.drawText(px.rect(), Qt.AlignmentFlag.AlignCenter, ">_")
    p.end()
    return QIcon(px)


def run_gui() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("ai-coder")
    app.setOrganizationName("AILinux")
    unified_mode = os.environ.get("AILINUX_LOOM_UNIFIED") == "1" or "--loom-unified" in sys.argv
    app.setQuitOnLastWindowClosed(unified_mode)

    icon = _make_icon()
    app.setWindowIcon(icon)

    from .main_window import MainWindow
    window = MainWindow()
    window.setWindowIcon(icon)

    # ── System Tray ─────────────────────────────────────
    tray = QSystemTrayIcon(icon, app)
    tray_menu = QMenu()

    # Open
    open_action = QAction("Oeffnen", tray)
    open_action.triggered.connect(window.show_and_raise)
    tray_menu.addAction(open_action)

    # Close (minimize to tray)
    close_action = QAction("Minimieren", tray)
    close_action.triggered.connect(window.hide)
    tray_menu.addAction(close_action)

    tray_menu.addSeparator()

    # Independent AILinux Helper companion.  AICoder is the primary app; the
    # helper remains a separate process and can also be run on its own.
    helper_menu = tray_menu.addMenu("AILinux Helper")
    helper_state_action = QAction("Status", tray)
    helper_state_action.setEnabled(False)
    helper_menu.addAction(helper_state_action)

    def _refresh_helper_state():
        state = helper_status()
        if state["managed_running"]:
            text = "Status: läuft (AICoder)"
        elif state["available"]:
            text = f"Status: verfügbar ({state['source']})"
        else:
            text = "Status: nicht gefunden"
        helper_state_action.setText(text)

    def _helper_message(result):
        ok, message = result
        tray.showMessage(
            "AILinux Helper", message,
            tray.MessageIcon.Information if ok else tray.MessageIcon.Warning, 3000,
        )
        _refresh_helper_state()

    helper_start_action = QAction("Im Hintergrund starten", tray)
    helper_start_action.triggered.connect(lambda: _helper_message(start_helper()))
    helper_menu.addAction(helper_start_action)

    helper_open_action = QAction("Öffnen", tray)
    helper_open_action.triggered.connect(lambda: _helper_message(open_helper()))
    helper_menu.addAction(helper_open_action)

    helper_stop_action = QAction("Von AICoder gestarteten Helper stoppen", tray)
    helper_stop_action.triggered.connect(lambda: _helper_message(stop_managed_helper()))
    helper_menu.addAction(helper_stop_action)
    helper_menu.aboutToShow.connect(_refresh_helper_state)
    _refresh_helper_state()

    tray_menu.addSeparator()

    # Start with OS (toggle)
    autostart_action = QAction("Mit System starten", tray)
    autostart_action.setCheckable(True)
    autostart_action.setChecked(is_autostart_enabled())
    def _toggle_autostart():
        new_state = toggle_autostart()
        autostart_action.setChecked(new_state)
        tray.showMessage(
            "ai-coder",
            "Autostart aktiviert" if new_state else "Autostart deaktiviert",
            tray.MessageIcon.Information, 2000,
        )
    autostart_action.triggered.connect(_toggle_autostart)
    tray_menu.addAction(autostart_action)

    tray_menu.addSeparator()

    # Quit
    quit_action = QAction("Beenden", tray)
    quit_action.triggered.connect(app.quit)
    tray_menu.addAction(quit_action)

    tray.setContextMenu(tray_menu)
    tray.activated.connect(lambda reason: (
        window.show_and_raise()
        if reason == QSystemTrayIcon.ActivationReason.Trigger
        else None
    ))
    tray.setToolTip("AILinux App · AICoder")
    if not unified_mode:
        tray.show()

    window.tray = None if unified_mode else tray
    window.show()

    # Shared Notify is opt-in. Once enabled, the GUI owns a lightweight daemon
    # heartbeat/mailbox worker for this machine and stops it on application exit.
    try:
        from ..shared_notify import start_background, stop_background
        start_background(interval=15)
        app.aboutToQuit.connect(stop_background)
    except Exception:
        pass

    return app.exec()
