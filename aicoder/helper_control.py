"""Launch/control the independent AILinux Helper from AICoder.

The helper remains a separate application/process.  AICoder may start it and
open its window, but only stops a helper process that this AICoder instance
started itself.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
from typing import Sequence


@dataclass(frozen=True)
class HelperTarget:
    command: tuple[str, ...]
    cwd: Path | None = None
    source: str = ""


_managed_process: subprocess.Popen | None = None


def _source_target(root: Path, *, background: bool) -> HelperTarget | None:
    desktop = root / "apps" / "desktop"
    package = desktop / "package.json"
    if not package.is_file() or shutil.which("npm") is None:
        return None
    args = ("npm", "start") + (("--", "--background") if background else ())
    return HelperTarget(args, desktop, "source")


def discover_helper(*, background: bool = True) -> HelperTarget | None:
    """Find a packaged helper or a source checkout without mutating the host."""
    command_override = os.environ.get("AILINUX_HELPER_COMMAND", "").strip()
    if command_override:
        parts = tuple(shlex.split(command_override, posix=platform.system() != "Windows"))
        if parts:
            return HelperTarget(parts + (("--background",) if background else ()), None, "env-command")

    executable_override = os.environ.get("AILINUX_HELPER_EXECUTABLE", "").strip()
    if executable_override:
        exe = Path(executable_override).expanduser()
        if exe.is_file():
            return HelperTarget((str(exe),) + (("--background",) if background else ()), exe.parent, "env-executable")

    root_override = os.environ.get("AILINUX_HELPER_ROOT", "").strip()
    if root_override:
        found = _source_target(Path(root_override).expanduser(), background=background)
        if found:
            return found

    packaged_names = (
        ("AILinux Helper.exe", "ailinux-helper.exe")
        if platform.system() == "Windows"
        else ("ailinux-helper", "AILinux Helper")
    )
    exe_dir = Path(sys.executable).resolve().parent
    for name in packaged_names:
        candidate = exe_dir / name
        if candidate.is_file():
            return HelperTarget((str(candidate),) + (("--background",) if background else ()), candidate.parent, "sibling")

    path_exe = shutil.which("ailinux-helper")
    if path_exe:
        return HelperTarget((path_exe,) + (("--background",) if background else ()), None, "path")

    # Developer layout: ~/ai-coder and ~/ailinux-helper as sibling checkouts.
    checkout_root = Path(__file__).resolve().parents[1]
    sibling_repo = checkout_root.parent / "ailinux-helper"
    found = _source_target(sibling_repo, background=background)
    if found:
        return found
    return None


def _spawn(target: HelperTarget) -> subprocess.Popen:
    kwargs: dict = {
        "cwd": str(target.cwd) if target.cwd else None,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": platform.system() != "Windows",
    }
    if platform.system() == "Windows":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(list(target.command), **kwargs)


def managed_running() -> bool:
    return _managed_process is not None and _managed_process.poll() is None


def start_helper() -> tuple[bool, str]:
    """Start Helper in background, reusing our managed child when possible."""
    global _managed_process
    if managed_running():
        return True, "AILinux Helper läuft bereits (von AICoder gestartet)."
    target = discover_helper(background=True)
    if target is None:
        return False, "AILinux Helper wurde nicht gefunden."
    try:
        _managed_process = _spawn(target)
    except OSError as exc:
        _managed_process = None
        return False, f"AILinux Helper konnte nicht gestartet werden: {exc}"
    # Electron may exit immediately after handing off to an already-running
    # single-instance Helper.  That is success, but it is not our process to stop.
    try:
        _managed_process.wait(timeout=0.25)
    except subprocess.TimeoutExpired:
        return True, f"AILinux Helper gestartet ({target.source})."
    _managed_process = None
    return True, f"AILinux Helper ist aktiv bzw. wurde an eine bestehende Instanz übergeben ({target.source})."


def open_helper() -> tuple[bool, str]:
    """Open Helper UI. Electron single-instance handling raises an existing Helper."""
    target = discover_helper(background=False)
    if target is None:
        return False, "AILinux Helper wurde nicht gefunden."
    try:
        _spawn(target)
    except OSError as exc:
        return False, f"AILinux Helper konnte nicht geöffnet werden: {exc}"
    return True, f"AILinux Helper geöffnet ({target.source})."


def stop_managed_helper() -> tuple[bool, str]:
    """Stop only the helper child owned by this AICoder instance."""
    global _managed_process
    if not managed_running():
        _managed_process = None
        return False, "Kein von AICoder gestarteter Helper läuft."
    assert _managed_process is not None
    try:
        _managed_process.terminate()
    except OSError as exc:
        return False, f"AILinux Helper konnte nicht beendet werden: {exc}"
    _managed_process = None
    return True, "Von AICoder gestarteter AILinux Helper wurde beendet."


def helper_status() -> dict[str, object]:
    target = discover_helper(background=True)
    return {
        "available": target is not None,
        "managed_running": managed_running(),
        "source": target.source if target else "",
        "command": list(target.command) if target else [],
    }
