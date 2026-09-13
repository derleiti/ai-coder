"""Shared visual language for AICoder, projected from canonical AILinux Loom tokens."""
from __future__ import annotations

import json
import re
from importlib.resources import files


def _tokens(mode: str = "dark") -> dict[str, str]:
    if mode not in {"dark", "light"}:
        raise ValueError(mode)
    doc = json.loads(files("aicoder.gui").joinpath("design_tokens.json").read_text(encoding="utf-8"))
    return dict(doc["modes"][mode])


def _qss(value: str) -> str:
    match = re.fullmatch(r"rgba\((\d+),(\d+),(\d+),([01](?:\.\d+)?)\)", value.replace(" ", ""))
    if not match:
        return value
    r,g,b=(int(match.group(i)) for i in range(1,4)); alpha=round(float(match.group(4))*255)
    return f"rgba({r},{g},{b},{alpha})"


_TEMPLATE = r'''
QMainWindow, QWidget#AppRoot {{ background: {background}; color: {text}; }}
QWidget#SettingsContent, QWidget#SettingsViewport, QScrollArea#SettingsScroll {{ background: {surface}; }}
QWidget {{ font-family: "Inter", "Noto Sans", sans-serif; font-size: 13px; }}
QLabel {{ color: {text_muted}; background: transparent; }}
QLabel#Brand {{ color: {text}; font-size: 17px; font-weight: 700; }}
QLabel#BrandMark {{ color: {accent}; font-size: 20px; font-weight: 800; }}
QLabel#Caption {{ color: {text_muted}; font-size: 11px; }}
QFrame#TopBar {{ background: {glass}; border: 1px solid {glass_border}; border-radius: 10px; }}
QTabWidget::pane {{ border: 1px solid {glass_border}; border-radius: 10px; background: {surface}; top: -1px; }}
QTabBar::tab {{ background: transparent; color: {text_muted}; padding: 10px 22px; border: none; border-bottom: 2px solid transparent; margin-right: 4px; }}
QTabBar::tab:selected {{ color: {text}; border-bottom-color: {accent}; }}
QTabBar::tab:hover {{ color: {accent}; }}
QGroupBox {{ color: {text}; background: {glass}; border: 1px solid {glass_border}; border-radius: 10px; margin-top: 14px; padding: 20px 14px 14px 14px; font-weight: 650; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 14px; padding: 0 7px; color: {text}; background: {background}; }}
QLineEdit, QPlainTextEdit, QComboBox, QSpinBox {{ background: {surface}; color: {text}; border: 1px solid {glass_border}; border-radius: 7px; padding: 7px 10px; selection-background-color: {accent}; }}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus {{ border-color: {accent}; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox QAbstractItemView {{ background: {surface}; color: {text}; border: 1px solid {glass_border}; selection-background-color: {glass}; padding: 4px; }}
QListWidget {{ background: {surface}; alternate-background-color: {glass}; color: {text}; border: 1px solid {glass_border}; border-radius: 8px; padding: 5px; outline: none; }}
QListWidget::item {{ padding: 6px 8px; border-radius: 4px; }}
QListWidget::item:selected {{ background: {glass}; color: {text}; }}
QPushButton {{ background: {surface}; color: {text}; border: 1px solid {glass_border}; border-radius: 7px; padding: 7px 14px; font-weight: 600; }}
QPushButton:hover {{ background: {glass}; color: {text}; border-color: {accent}; }}
QPushButton:disabled {{ color: {text_muted}; }}
QPushButton#PrimaryButton {{ background: {accent}; color: {background}; border-color: {accent}; padding-left: 20px; padding-right: 20px; }}
QPushButton#PrimaryButton:hover {{ background: {accent_hover}; border-color: {accent_hover}; }}
QPushButton#DangerButton {{ color: {error}; }}
QTextEdit#ChatLog {{ background: {surface}; color: {text}; border: 1px solid {glass_border}; border-radius: 9px; padding: 12px; font-family: "Cascadia Code", "JetBrains Mono", "Fira Code", monospace; font-size: 13px; }}
QScrollArea {{ border: none; background: {surface}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {glass_border}; border-radius: 5px; min-height: 28px; }}
QToolTip {{ background: {surface}; color: {text}; border: 1px solid {glass_border}; padding: 5px; }}
'''


def stylesheet_for(mode: str = "dark") -> str:
    values={key.replace("-","_"):_qss(value) for key,value in _tokens(mode).items()}
    return _TEMPLATE.format(**values)

APP_STYLESHEET = stylesheet_for("dark")
