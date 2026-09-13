from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from aicoder import helper_control as hc


def test_source_target_uses_background_flag(tmp_path: Path):
    desktop = tmp_path / "apps" / "desktop"
    desktop.mkdir(parents=True)
    (desktop / "package.json").write_text("{}")
    with patch("aicoder.helper_control.shutil.which", return_value="/usr/bin/npm"):
        target = hc._source_target(tmp_path, background=True)
    assert target is not None
    assert target.command == ("npm", "start", "--", "--background")
    assert target.cwd == desktop


def test_env_helper_root_is_preferred(tmp_path: Path):
    desktop = tmp_path / "apps" / "desktop"
    desktop.mkdir(parents=True)
    (desktop / "package.json").write_text("{}")
    with (
        patch.dict("os.environ", {"AILINUX_HELPER_ROOT": str(tmp_path)}, clear=True),
        patch("aicoder.helper_control.shutil.which", return_value="/usr/bin/npm"),
    ):
        target = hc.discover_helper(background=True)
    assert target is not None
    assert target.source == "source"


def test_stop_never_kills_unmanaged_helper():
    hc._managed_process = None
    ok, message = hc.stop_managed_helper()
    assert ok is False
    assert "Kein von AICoder" in message


def test_gui_tray_exposes_helper_controls():
    source = Path("aicoder/gui/app.py").read_text(encoding="utf-8")
    assert 'tray_menu.addMenu("AILinux Helper")' in source
    assert 'start_helper()' in source
    assert 'open_helper()' in source
    assert 'stop_managed_helper()' in source


def test_start_handoff_does_not_claim_existing_single_instance():
    class ExitedProcess:
        def wait(self, timeout=None):
            return 0
        def poll(self):
            return 0

    target = hc.HelperTarget(("helper",), None, "test")
    hc._managed_process = None
    with (
        patch("aicoder.helper_control.discover_helper", return_value=target),
        patch("aicoder.helper_control._spawn", return_value=ExitedProcess()),
    ):
        ok, message = hc.start_helper()
    assert ok is True
    assert "bestehende Instanz" in message
    assert hc._managed_process is None
