# -*- mode: python ; coding: utf-8 -*-
block_cipher = None

a = Analysis(
    ['aicoder_main.py'],
    pathex=['/home/zombie/ai-coder'],
    binaries=[],
    datas=[],
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
        'getpass',
        'threading',
        'subprocess',
        'difflib',
        'pathlib',
        'json',
        'shutil',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', 'PyQt5', 'PyQt6', 'wx', 'matplotlib'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
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
    strip=True,
    upx=False,
    console=True,
    onefile=True,
)
