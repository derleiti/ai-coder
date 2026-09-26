from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
from pathlib import Path

import pytest

from aicoder.evidence_memory import ProjectEvidenceStore
from aicoder.project_memory import LocalProjectMemory, project_identity


@pytest.fixture(autouse=True)
def fixed_device(monkeypatch):
    monkeypatch.setenv("AICODER_DEVICE_ID", "test-device")


def init_git(path: Path, remote: str) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", remote], check=True)


def accepted(item: dict, *, version: int, seq: int, deleted: bool = False) -> dict:
    return {
        "entity_id": item["entity_id"],
        "kind": item["kind"],
        "title": item["title"],
        "content": "" if deleted else item["content"],
        "status": item["status"],
        "version": version,
        "base_version": max(0, version - 1),
        "server_seq": seq,
        "content_hash": item["content_hash"],
        "device_id": item["device_id"],
        "source": item["source"],
        "updated_at": item["updated_at"],
        "deleted": deleted,
    }


def test_project_identity_stable_for_same_git_remote_after_path_move(tmp_path):
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir(); two.mkdir()
    init_git(one, "git@github.com:Example/Repo.git")
    init_git(two, "https://github.com/example/repo.git")
    assert project_identity(one) == project_identity(two)


def test_local_storage_survives_store_restart_and_search(tmp_path):
    root = tmp_path / "repo"; root.mkdir()
    init_git(root, "https://github.com/example/project.git")
    db = tmp_path / "evidence.db"

    first = LocalProjectMemory(root, db)
    item = first.store(kind="todo", title="Provider fallback", content="Refactor fallback later")
    assert item["sync_state"] == "dirty"

    restored = LocalProjectMemory(root, db)
    rows = restored.search("provider fallback")
    assert len(rows) == 1
    assert rows[0]["entity_id"] == item["entity_id"]
    assert restored.dirty_changes()[0]["entity_id"] == item["entity_id"]


def test_sync_accepts_push_pull_and_persists_cursor(tmp_path):
    root = tmp_path / "repo"; root.mkdir()
    init_git(root, "https://github.com/example/project.git")
    memory = LocalProjectMemory(root, tmp_path / "evidence.db")
    local = memory.store(kind="decision", title="Use OCC", content="server_seq orders accepted revisions")

    class Client:
        def project_memory_sync(self, payload):
            assert payload["cursor"] == 0
            assert payload["changes"][0]["entity_id"] == local["entity_id"]
            own = accepted(payload["changes"][0], version=1, seq=10)
            remote = {
                **own,
                "entity_id": "remote-1",
                "kind": "architecture",
                "title": "Canonical server",
                "content": "TriForce is sync authority",
                "version": 2,
                "base_version": 1,
                "server_seq": 11,
            }
            return {"cursor": 11, "accepted": [own], "conflicts": [], "remote": [own, remote]}

    result = memory.sync(Client())
    assert result["status"] == "ok"
    assert memory.cursor() == 11
    assert memory.get(local["entity_id"])["sync_state"] == "clean"
    assert memory.get(local["entity_id"])["version"] == 1
    assert memory.get("remote-1")["content"] == "TriForce is sync authority"


def test_conflict_is_persisted_without_losing_local_content(tmp_path):
    root = tmp_path / "repo"; root.mkdir()
    init_git(root, "https://github.com/example/project.git")
    memory = LocalProjectMemory(root, tmp_path / "evidence.db")
    local = memory.store(kind="todo", title="Conflict", content="my local text")

    memory.apply_sync({
        "cursor": 7,
        "accepted": [],
        "remote": [],
        "conflicts": [{
            "entity_id": local["entity_id"],
            "attempted_revision_seq": 6,
            "attempted": local,
            "current": {
                **accepted(local, version=3, seq=7),
                "content": "server text",
            },
        }],
    })

    row = memory.get(local["entity_id"])
    assert row["content"] == "my local text"
    assert row["sync_state"] == "conflict"
    assert row["version"] == 3
    conflicts = memory.conflicts()
    assert conflicts[0]["remote"]["content"] == "server text"
    assert conflicts[0]["local"]["content"] == "my local text"


def test_tombstone_propagation(tmp_path):
    root = tmp_path / "repo"; root.mkdir()
    init_git(root, "https://github.com/example/project.git")
    memory = LocalProjectMemory(root, tmp_path / "evidence.db")
    item = memory.store(kind="idea", title="Remove me", content="temporary")
    memory.apply_sync({
        "cursor": 1,
        "accepted": [accepted(item, version=1, seq=1)],
        "conflicts": [],
        "remote": [],
    })
    deleted = memory.delete(item["entity_id"])
    assert deleted["deleted"] is True
    assert memory.dirty_changes()[0]["deleted"] is True

    tombstone = accepted(deleted, version=2, seq=2, deleted=True)
    memory.apply_sync({"cursor": 2, "accepted": [tombstone], "conflicts": [], "remote": [tombstone]})
    assert memory.list() == []
    assert memory.list(include_deleted=True)[0]["deleted"] is True


def test_network_failure_does_not_modify_dirty_state_or_cursor(tmp_path):
    root = tmp_path / "repo"; root.mkdir()
    init_git(root, "https://github.com/example/project.git")
    memory = LocalProjectMemory(root, tmp_path / "evidence.db")
    item = memory.store(kind="bug", title="Offline", content="Keep local")

    class Broken:
        def project_memory_sync(self, payload):
            raise OSError("offline")

    with pytest.raises(OSError):
        memory.sync(Broken())
    assert memory.cursor() == 0
    assert memory.get(item["entity_id"])["sync_state"] == "dirty"


def test_secret_material_is_rejected_and_not_written(tmp_path):
    root = tmp_path / "repo"; root.mkdir()
    init_git(root, "https://github.com/example/project.git")
    db = tmp_path / "evidence.db"
    memory = LocalProjectMemory(root, db)
    with pytest.raises(ValueError):
        memory.store(kind="documentation", title="bad", content="Authorization: Bearer super-secret-token")
    assert b"super-secret-token" not in db.read_bytes()


def test_feature_memory_search_remains_functional_in_same_db(tmp_path):
    root = tmp_path / "repo"; root.mkdir()
    init_git(root, "https://github.com/example/project.git")
    db = tmp_path / "evidence.db"
    evidence = ProjectEvidenceStore(str(root), db)
    evidence.remember_feature_experience(
        task="Project memory integration",
        summary="Kept evidence database compatible",
        architecture="evidence.db -> feature_experience + project_memory",
        verification="tests pass",
        lessons="Do not delete user history",
        future_features="Conflict UI",
    )

    LocalProjectMemory(root, db).store(kind="feature", title="Memory sync", content="Local-first")
    rows = ProjectEvidenceStore(str(root), db).search_feature_experience("project memory", limit=5)
    assert len(rows) == 1
    assert "evidence.db" in rows[0].architecture

def test_agent_project_memory_tools_store_update_search_list_and_offline_sync(tmp_path, monkeypatch):
    import json
    from aicoder import project_memory as pm
    from aicoder.executor import LOCAL_TOOL_NAMES, run_tool

    root = tmp_path / "repo"; root.mkdir()
    init_git(root, "https://github.com/example/tools.git")
    monkeypatch.setattr(pm, "DB_PATH", tmp_path / "evidence.db")

    class Client:
        def project_memory_sync(self, payload):
            raise OSError("offline")

    client = Client()
    expected = {
        "project_memory_store", "project_memory_update", "project_memory_search",
        "project_memory_list", "project_memory_sync",
    }
    assert expected <= LOCAL_TOOL_NAMES

    raw, err = run_tool(
        client, "project_memory_store",
        {"kind": "todo", "title": "Provider fallback", "content": "Refactor later"},
        workspace_root=root,
    )
    assert not err
    stored = json.loads(raw)
    entity_id = stored["entity_id"]

    raw, err = run_tool(
        client, "project_memory_update",
        {"entity_id": entity_id, "content": "Refactor with OCC"},
        workspace_root=root,
    )
    assert not err
    assert json.loads(raw)["content"] == "Refactor with OCC"

    raw, err = run_tool(
        client, "project_memory_search", {"query": "OCC"}, workspace_root=root,
    )
    assert not err
    assert json.loads(raw)[0]["entity_id"] == entity_id

    raw, err = run_tool(
        client, "project_memory_list", {}, workspace_root=root,
    )
    assert not err
    assert json.loads(raw)[0]["entity_id"] == entity_id

    raw, err = run_tool(
        client, "project_memory_sync", {}, workspace_root=root,
    )
    assert not err
    status = json.loads(raw)
    assert status["status"] == "offline"
    assert status["dirty"] == 1
