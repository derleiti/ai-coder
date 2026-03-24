"""QApplication + System Tray fuer ai-coder GUI."""
from __future__ import annotations
import sys
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon, QMenu
from PyQt6.QtGui import QIcon, QPixmap, QPainter, QColor, QAction
from PyQt6.QtCore import Qt


def _make_icon() -> QIcon:
    """Generiert ein einfaches App-Icon."""
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
    """Startet die GUI-Applikation."""
    app = QApplication(sys.argv)
    app.setApplicationName("ai-coder")
    app.setOrganizationName("AILinux")
    app.setQuitOnLastWindowClosed(False)

    icon = _make_icon()
    app.setWindowIcon(icon)

    from .main_window import MainWindow

    window = MainWindow()
    window.setWindowIcon(icon)

    # System Tray
    tray = QSystemTrayIcon(icon, app)
    tray_menu = QMenu()

    show_action = QAction("Oeffnen", tray)
    show_action.triggered.connect(window.show_and_raise)
    tray_menu.addAction(show_action)

    tray_menu.addSeparator()

    quit_action = QAction("Beenden", tray)
    quit_action.triggered.connect(app.quit)
    tray_menu.addAction(quit_action)

    tray.setContextMenu(tray_menu)
    tray.activated.connect(lambda reason: (
        window.show_and_raise()
        if reason == QSystemTrayIcon.ActivationReason.Trigger
        else None
    ))
    tray.setToolTip("ai-coder")
    tray.show()

    window.tray = tray
    window.show()

    return app.exec()
