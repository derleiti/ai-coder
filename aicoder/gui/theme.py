"""Shared visual language for the ai-coder desktop client."""

APP_STYLESHEET = r"""
QMainWindow, QWidget#AppRoot {
    background: #0b0f16;
    color: #dce5f2;
}
QWidget#SettingsContent, QWidget#SettingsViewport, QScrollArea#SettingsScroll {
    background: #0d121b;
}
QWidget { font-family: "Inter", "Noto Sans", sans-serif; font-size: 13px; }
QLabel { color: #aeb9c9; background: transparent; }
QLabel#Brand { color: #f1f6ff; font-size: 17px; font-weight: 700; }
QLabel#BrandMark { color: #43d9c0; font-size: 20px; font-weight: 800; }
QLabel#Caption { color: #718096; font-size: 11px; }
QFrame#TopBar {
    background: #0f1520;
    border: 1px solid #202a38;
    border-radius: 10px;
}
QTabWidget::pane {
    border: 1px solid #202a38;
    border-radius: 10px;
    background: #0d121b;
    top: -1px;
}
QTabBar::tab {
    background: transparent;
    color: #7f8ca0;
    padding: 10px 22px;
    border: none;
    border-bottom: 2px solid transparent;
    margin-right: 4px;
}
QTabBar::tab:selected { color: #e8f0fa; border-bottom-color: #43d9c0; }
QTabBar::tab:hover { color: #43d9c0; }
QGroupBox {
    color: #e4ebf5;
    background: #0f1520;
    border: 1px solid #253142;
    border-radius: 10px;
    margin-top: 14px;
    padding: 20px 14px 14px 14px;
    font-weight: 650;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 14px;
    padding: 0 7px;
    color: #cdd7e5;
    background: #0b0f16;
}
QLineEdit, QPlainTextEdit, QComboBox, QSpinBox {
    background: #090d13;
    color: #e8eef7;
    border: 1px solid #2b3748;
    border-radius: 7px;
    padding: 7px 10px;
    selection-background-color: #1f665e;
}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus {
    border-color: #43d9c0;
}
QComboBox::drop-down { border: none; width: 24px; }
QComboBox QAbstractItemView {
    background: #101722;
    color: #dce5f2;
    border: 1px solid #314055;
    selection-background-color: #1d4f4a;
    padding: 4px;
}
QListWidget {
    background: #090d13;
    alternate-background-color: #0d131d;
    color: #bdc8d8;
    border: 1px solid #263244;
    border-radius: 8px;
    padding: 5px;
    outline: none;
}
QListWidget::item { padding: 6px 8px; border-radius: 4px; }
QListWidget::item:selected { background: #193b3a; color: #effffc; }
QPushButton {
    background: #182130;
    color: #cbd5e3;
    border: 1px solid #334157;
    border-radius: 7px;
    padding: 7px 14px;
    font-weight: 600;
}
QPushButton:hover { background: #202d40; color: #ffffff; border-color: #4b5e78; }
QPushButton:pressed { background: #111a27; }
QPushButton:disabled { background: #111722; color: #526074; border-color: #222c3a; }
QPushButton#PrimaryButton {
    background: #43d9c0;
    color: #07110f;
    border-color: #43d9c0;
    padding-left: 20px;
    padding-right: 20px;
}
QPushButton#PrimaryButton:hover { background: #62ead4; border-color: #62ead4; }
QPushButton#DangerButton { color: #ff7685; }
QPushButton#DangerButton:hover { background: #351a23; border-color: #b94f61; }
QTextEdit#ChatLog {
    background: #080c12;
    color: #dce5f2;
    border: 1px solid #202b3a;
    border-radius: 9px;
    padding: 12px;
    font-family: "Cascadia Code", "JetBrains Mono", "Fira Code", monospace;
    font-size: 13px;
}
QScrollArea { border: none; background: #0d121b; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #344258; border-radius: 5px; min-height: 28px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QToolTip { background: #17202e; color: #edf4ff; border: 1px solid #3b4b62; padding: 5px; }
"""
