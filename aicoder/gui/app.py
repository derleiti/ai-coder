"""QApplication + System Tray mit erweitertem Menue."""
from __future__ import annotations
import sys
import platform

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
    app.setQuitOnLastWindowClosed(False)

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
    tray.setToolTip("ai-coder — Terminal Coding & DevOps Agent")
    tray.show()

    window.tray = tray
    window.show()

    return app.exec()
