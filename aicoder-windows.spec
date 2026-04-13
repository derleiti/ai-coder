# -*- mode: python ; coding: utf-8 -*-
# Windows-spezifisches PyInstaller .spec
# Build: pyinstaller aicoder-windows.spec

import sys
from pathlib import Path

block_cipher = None

# Qt6-Plattform-Plugins für PyQt6 auf Windows (zwingend für GUI)
try:
    import PyQt6
    _qt_platforms = Path(PyQt6.__file__).parent / "Qt6" / "plugins" / "platforms"
    platform_datas = [(str(_qt_platforms), "PyQt6/Qt6/plugins/platforms")] if _qt_platforms.exists() else []
except ImportError:
    platform_datas = []

a = Analysis(
    ['aicoder_main.py'],
    pathex=['.'],
    binaries=[],
    datas=platform_datas,
    hiddenimports=[
        'aicoder.cli',
        'aicoder.client',
        'aicoder.config',
        'aicoder.session_state',
        'aicoder.docs_context',
        'aicoder.history',
        'aicoder.status',
        'aicoder.workspace',
        'aicoder.task',
        'aicoder.swarm_runner',
        'aicoder.agent',
        'aicoder.setup',
        'aicoder.ui',
        'aicoder.executor',
        'aicoder.audit',
        'aicoder.gui',
        'aicoder.gui.app',
        'aicoder.gui.main_window',
        'aicoder.gui.chat_widget',
        'aicoder.gui.settings_widget',
        'aicoder.gui.autostart',
        'PyQt6',
        'PyQt6.QtWidgets',
        'PyQt6.QtGui',
        'PyQt6.QtCore',
        'PyQt6.QtNetwork',
        'certifi',
        'markdown',
        'markdown.extensions.fenced_code',
        'markdown.extensions.nl2br',
        'getpass',
        'threading',
        'subprocess',
        'difflib',
        'pathlib',
        'json',
        'shutil',
        'winreg',      # Windows Registry (für Autostart etc.)
        'ctypes',
        'ctypes.wintypes',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', 'PyQt5', 'wx', 'matplotlib', 'pyte'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='aicoder',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,           # kein strip auf Windows
    upx=False,
    console=True,          # Terminal-App bleibt Console
    onefile=True,
    # icon='installer/aicoder.ico',  # aktivieren sobald .ico vorhanden
)
