"""Hauptfenster fuer ai-coder GUI — Tabs: Chat + Settings."""
from __future__ import annotations
from PyQt6.QtWidgets import QMainWindow, QTabWidget
from PyQt6.QtCore import Qt, QSize

from .chat_widget import ChatWidget
from .settings_widget import SettingsWidget


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.tray = None  # wird von app.py gesetzt
        self.setWindowTitle("ai-coder")
        self.setMinimumSize(QSize(600, 450))
        self.resize(800, 600)

        self._apply_style()

        # Tabs
        self.tabs = QTabWidget()
        self.settings_tab = SettingsWidget()
        self.chat_tab = ChatWidget(settings_ref=self.settings_tab)

        self.tabs.addTab(self.chat_tab, "Chat")
        self.tabs.addTab(self.settings_tab, "Settings")

        self.setCentralWidget(self.tabs)

    def _apply_style(self):
        self.setStyleSheet("""
            QMainWindow { background: #0d0d1a; }
            QTabWidget::pane {
                border: 1px solid #333;
                background: #0d0d1a;
            }
            QTabBar::tab {
                background: #1a1a2e;
                color: #aaa;
                padding: 8px 20px;
                border: 1px solid #333;
                border-bottom: none;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
                margin-right: 2px;
            }
            QTabBar::tab:selected {
                background: #0d0d1a;
                color: #00d4ff;
                border-bottom: 2px solid #00d4ff;
            }
            QTabBar::tab:hover { color: #fff; }
            QGroupBox {
                color: #ccc;
                border: 1px solid #333;
                border-radius: 6px;
                margin-top: 12px;
                padding-top: 16px;
                font-weight: bold;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 6px;
            }
            QLabel { color: #ccc; }
            QLineEdit {
                background: #111;
                color: #fff;
                border: 1px solid #444;
                border-radius: 4px;
                padding: 6px 10px;
            }
            QLineEdit:focus { border-color: #00d4ff; }
            QComboBox {
                background: #111;
                color: #fff;
                border: 1px solid #444;
                border-radius: 4px;
                padding: 6px 10px;
            }
            QPushButton {
                background: #1a1a2e;
                color: #ccc;
                border: 1px solid #444;
                border-radius: 4px;
                padding: 6px 16px;
            }
            QPushButton:hover { background: #252545; color: #fff; }
        """)

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
            event.accept()

    def show_and_raise(self):
        self.showNormal()
        self.activateWindow()
        self.raise_()
