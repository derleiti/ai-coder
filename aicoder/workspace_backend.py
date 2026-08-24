"""Execution workspace backends for the experimental AICoder runtime.

Phase 1 keeps current behaviour through DiskWorkspace.  RAM/copy-on-write
backends can implement the same contract without changing agent/tool semantics.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class WorkspaceInfo:
    mode: str
    source_root: Path
    execution_root: Path
    volatile: bool = False
    transactional: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "source_root": str(self.source_root),
            "execution_root": str(self.execution_root),
            "volatile": self.volatile,
            "transactional": self.transactional,
        }


class WorkspaceBackend(ABC):
    """Lifecycle boundary between the user's source tree and agent execution."""

    @property
    @abstractmethod
    def info(self) -> WorkspaceInfo:
        raise NotImplementedError

    @abstractmethod
    def prepare(self) -> Path:
        """Return the workspace path tools should operate on."""
        raise NotImplementedError

    @abstractmethod
    def finalize(self, *, verified: bool) -> None:
        """Finalize execution; future transactional backends may commit here."""
        raise NotImplementedError

    @abstractmethod
    def abort(self) -> None:
        """Discard volatile execution state where applicable."""
        raise NotImplementedError


class DiskWorkspace(WorkspaceBackend):
    """Compatibility backend: operate directly on the existing workspace."""

    def __init__(self, root: str | Path):
        resolved = Path(root).expanduser().resolve(strict=False)
        if not resolved.exists() or not resolved.is_dir():
            raise ValueError(f"workspace is not an existing directory: {resolved}")
        self._info = WorkspaceInfo(
            mode="disk",
            source_root=resolved,
            execution_root=resolved,
            volatile=False,
            transactional=False,
        )

    @property
    def info(self) -> WorkspaceInfo:
        return self._info

    def prepare(self) -> Path:
        return self._info.execution_root

    def finalize(self, *, verified: bool) -> None:
        return None

    def abort(self) -> None:
        return None
