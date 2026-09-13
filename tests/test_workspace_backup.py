from __future__ import annotations

import subprocess
import tarfile
from pathlib import Path

from aicoder.workspace_backup import snapshot_workspace


def _members(archive: Path) -> set[str]:
    with tarfile.open(archive, "r:gz") as tar:
        return {m.name.rstrip("/") for m in tar.getmembers()}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_git_workspace_snapshot_keeps_source_and_ignored_local_config_but_skips_runtime_data(tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    _git(workspace, "init")
    (workspace / "src").mkdir()
    (workspace / "src" / "app.py").write_text("print('ok')\n")
    (workspace / ".gitignore").write_text("local.env\nruntime/\n")
    _git(workspace, "add", ".gitignore", "src/app.py")
    (workspace / "notes.txt").write_text("untracked source\n")
    (workspace / "local.env").write_text("LOCAL_ONLY=yes\n")
    runtime = workspace / "runtime"
    runtime.mkdir()
    (runtime / "database.bin").write_bytes(b"x" * 4096)

    monkeypatch.setenv("AILINUX_WORKSPACE_BACKUP_ROOT", str(tmp_path / "backups"))
    members = _members(snapshot_workspace(workspace, source="test"))

    assert "src/app.py" in members
    assert "notes.txt" in members
    assert "local.env" in members
    assert ".gitignore" in members
    assert "runtime/database.bin" not in members
    assert not any(item == ".git" or item.startswith(".git/") for item in members)


def test_non_git_workspace_snapshot_preserves_files_and_symlink_without_cross_following(tmp_path, monkeypatch):
    workspace = tmp_path / "plain"
    workspace.mkdir()
    (workspace / "data.txt").write_text("keep\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside\n")
    (workspace / "linked").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("AILINUX_WORKSPACE_BACKUP_ROOT", str(tmp_path / "backups"))

    archive = snapshot_workspace(workspace, source="test")
    with tarfile.open(archive, "r:gz") as tar:
        names = {m.name for m in tar.getmembers()}
        assert "data.txt" in names
        assert tar.getmember("linked").issym()
        assert "linked/secret.txt" not in names
