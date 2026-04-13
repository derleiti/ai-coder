"""clipboard.py — Cross-platform clipboard read/write for ai-coder agent."""
from __future__ import annotations
import platform
import subprocess
from typing import Tuple

IS_WINDOWS = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"


def clipboard_read() -> Tuple[str, bool]:
    """Read current clipboard content. Returns (text, is_error)."""
    try:
        if IS_WINDOWS:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
                capture_output=True, text=True, timeout=5,
            )
            return (r.stdout.strip() or "(clipboard empty)"), r.returncode != 0
        elif IS_MAC:
            r = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5)
            return (r.stdout.strip() or "(clipboard empty)"), r.returncode != 0
        else:
            # Linux: try xclip, then xsel, then wl-paste (Wayland)
            for cmd in (
                ["xclip", "-selection", "clipboard", "-o"],
                ["xsel", "--clipboard", "--output"],
                ["wl-paste"],
            ):
                try:
                    r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                    if r.returncode == 0:
                        return (r.stdout.strip() or "(clipboard empty)"), False
                except FileNotFoundError:
                    continue
            return "No clipboard tool found (install xclip, xsel, or wl-clipboard)", True
    except Exception as e:
        return f"clipboard_read error: {e}", True


def clipboard_write(text: str) -> Tuple[str, bool]:
    """Write text to clipboard. Returns (status, is_error)."""
    try:
        if IS_WINDOWS:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", f"Set-Clipboard -Value '{text}'"],
                capture_output=True, text=True, timeout=5,
            )
            return ("Copied to clipboard" if r.returncode == 0 else r.stderr), r.returncode != 0
        elif IS_MAC:
            r = subprocess.run(["pbcopy"], input=text, capture_output=True, text=True, timeout=5)
            return ("Copied to clipboard" if r.returncode == 0 else r.stderr), r.returncode != 0
        else:
            for cmd in (
                ["xclip", "-selection", "clipboard"],
                ["xsel", "--clipboard", "--input"],
                ["wl-copy"],
            ):
                try:
                    r = subprocess.run(cmd, input=text, capture_output=True, text=True, timeout=5)
                    if r.returncode == 0:
                        return "Copied to clipboard", False
                except FileNotFoundError:
                    continue
            return "No clipboard tool found (install xclip, xsel, or wl-clipboard)", True
    except Exception as e:
        return f"clipboard_write error: {e}", True
