"""Cross-platform autostart for ai-coder GUI."""
from __future__ import annotations
import os
import sys
import platform
from pathlib import Path

APP_NAME = "ai-coder"
DESKTOP_ENTRY = """[Desktop Entry]
Type=Application
Name=ai-coder
Comment=Terminal Coding & DevOps Agent
Exec={exec_path} gui
Icon=utilities-terminal
Terminal=false
Categories=Development;Utility;
StartupNotify=false
X-GNOME-Autostart-enabled=true
"""

def _linux_autostart_path() -> Path:
    return Path.home() / ".config" / "autostart" / "ai-coder.desktop"

def _linux_exec_path() -> str:
    # Prefer system binary, fallback to current executable
    for p in ["/usr/bin/aicoder", "/usr/local/bin/aicoder"]:
        if os.path.isfile(p):
            return p
    return sys.executable + " -m aicoder.cli"

def _win_registry_key_read():
    import winreg
    return winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Run",
        0, winreg.KEY_READ,
    )

def _win_registry_key_write():
    import winreg
    return winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Run",
        0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE,
    )

def is_autostart_enabled() -> bool:
    if platform.system() == "Linux":
        return _linux_autostart_path().exists()
    elif platform.system() == "Windows":
        try:
            import winreg
            key = _win_registry_key_read()
            winreg.QueryValueEx(key, APP_NAME)
            winreg.CloseKey(key)
            return True
        except (FileNotFoundError, OSError):
            return False
    return False

def enable_autostart() -> None:
    if platform.system() == "Linux":
        p = _linux_autostart_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(DESKTOP_ENTRY.format(exec_path=_linux_exec_path()))
    elif platform.system() == "Windows":
        import winreg
        exe = sys.executable
        # If running as PyInstaller bundle
        if getattr(sys, 'frozen', False):
            exe = sys.executable
        key = _win_registry_key_write()
        winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, f'"{exe}" gui')
        winreg.CloseKey(key)

def disable_autostart() -> None:
    if platform.system() == "Linux":
        p = _linux_autostart_path()
        if p.exists():
            p.unlink()
    elif platform.system() == "Windows":
        try:
            import winreg
            key = _win_registry_key_write()
            winreg.DeleteValue(key, APP_NAME)
            winreg.CloseKey(key)
        except (FileNotFoundError, OSError):
            pass

def toggle_autostart() -> bool:
    """Toggle autostart. Returns new state."""
    if is_autostart_enabled():
        disable_autostart()
        return False
    else:
        enable_autostart()
        return True
