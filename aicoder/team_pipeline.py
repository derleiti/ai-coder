"""Deterministic stage gates and project-aware verification for team runs."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
from pathlib import Path
import subprocess
import time
import uuid
from typing import Any, Iterable


class TeamStage(str, Enum):
    PLAN_RESEARCH = "plan_research"
    RESEARCH = "research"
    BRAINSTORM = "brainstorm"
    PLAN_CODE = "plan_code"
    CODE = "code"
    MERGE_PLAN = "merge_plan"
    MERGE = "merge"
    PLAN_TESTS = "plan_tests"
    TESTS_FUNCTION_OK = "tests_function_ok"
    ATOMIC_DISK_WRITE = "atomic_disk_write"


STAGE_ORDER = tuple(TeamStage)


@dataclass
class StageLedger:
    """Append-only in-memory stage ledger; persistence happens only through RAM artifacts."""
    completed: list[str] = field(default_factory=list)
    current: str = ""

    def start(self, stage: TeamStage) -> None:
        expected = STAGE_ORDER[len(self.completed)] if len(self.completed) < len(STAGE_ORDER) else None
        if expected != stage:
            raise RuntimeError(f"invalid team stage transition: expected {expected}, got {stage}")
        self.current = stage.value

    def complete(self, stage: TeamStage) -> None:
        if self.current != stage.value:
            raise RuntimeError(f"cannot complete inactive stage {stage.value}")
        self.completed.append(stage.value)
        self.current = ""

    def as_dict(self) -> dict[str, Any]:
        return {"completed": list(self.completed), "current": self.current}


@dataclass(frozen=True)
class VerificationCommand:
    name: str
    argv: tuple[str, ...]
    timeout: int = 180
    required: bool = True


@dataclass
class VerificationResult:
    name: str
    argv: list[str]
    ok: bool
    exit_code: int
    elapsed_ms: int
    output: str
    required: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "argv": self.argv, "ok": self.ok,
            "exit_code": self.exit_code, "elapsed_ms": self.elapsed_ms,
            "output": self.output, "required": self.required,
        }


_TEST_DIR_NAMES = {"test", "tests", "spec", "specs", "__tests__"}
_SOURCE_SUFFIXES = {".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".kts", ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".cs", ".rb", ".php", ".swift", ".scala", ".sh"}

def _is_test_path(path: str) -> bool:
    p = Path(path)
    lowered_parts = {part.lower() for part in p.parts}
    name = p.name.lower()
    stem = p.stem.lower()
    return bool(lowered_parts & _TEST_DIR_NAMES or name.startswith("test_") or stem.endswith("_test") or ".test." in name or ".spec." in name)

def test_change_evidence(delta: dict[str, Any]) -> dict[str, Any]:
    paths = sorted({str(path) for path in (delta.get("changed") or []) + (delta.get("deleted") or [])})
    test_paths = [path for path in paths if _is_test_path(path)]
    source_paths = [path for path in paths if not _is_test_path(path) and Path(path).suffix.lower() in _SOURCE_SUFFIXES]
    return {"source_paths": source_paths, "test_paths": test_paths, "behavior_change": bool(source_paths), "tests_changed": bool(test_paths), "coverage_evidence_ok": (not source_paths) or bool(test_paths)}

def project_verification_plan(root: str | Path) -> list[VerificationCommand]:
    """Infer deterministic checks from repository-native metadata, without an LLM vote."""
    root = Path(root)
    commands: list[VerificationCommand] = []

    if (root / "pyproject.toml").exists() or (root / "setup.py").exists() or (root / "setup.cfg").exists():
        commands.append(VerificationCommand("python-compile", ("python3", "-m", "compileall", "-q", "-x", r"(^|/)(\.aicoder-team|\.venv)(/|$)", "."), 120))
        if (root / "tests").is_dir():
            pyproject_text = ""
            if (root / "pyproject.toml").exists():
                pyproject_text = (root / "pyproject.toml").read_text(encoding="utf-8", errors="ignore")
            uses_pytest = (
                (root / "pytest.ini").exists()
                or (root / "conftest.py").exists()
                or "pytest" in pyproject_text.lower()
            )
            if uses_pytest:
                commands.append(VerificationCommand("python-tests", ("python3", "-m", "pytest", "-q"), 300))
            else:
                commands.append(VerificationCommand("python-tests", ("python3", "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"), 300))
        if (root / "ruff.toml").exists() or (root / ".ruff.toml").exists():
            commands.append(VerificationCommand("ruff", ("python3", "-m", "ruff", "check", "."), 180))
        if (root / "mypy.ini").exists() or (root / ".mypy.ini").exists():
            commands.append(VerificationCommand("mypy", ("python3", "-m", "mypy", "."), 240))

    if (root / "package.json").exists():
        try:
            package = json.loads((root / "package.json").read_text(encoding="utf-8"))
            scripts = package.get("scripts") if isinstance(package, dict) else {}
        except Exception:
            scripts = {}
        runner = "npm"
        if (root / "pnpm-lock.yaml").exists(): runner = "pnpm"
        elif (root / "yarn.lock").exists(): runner = "yarn"
        for script, label in (("test", "js-tests"), ("lint", "js-lint"), ("typecheck", "js-typecheck"), ("build", "js-build")):
            if isinstance(scripts, dict) and script in scripts:
                argv = (runner, script) if runner != "npm" else ("npm", "run", script)
                commands.append(VerificationCommand(label, argv, 300))

    if (root / "Cargo.toml").exists():
        commands.extend([
            VerificationCommand("cargo-check", ("cargo", "check", "--all-targets"), 300),
            VerificationCommand("cargo-test", ("cargo", "test", "--all-targets"), 300),
        ])
    if (root / "go.mod").exists():
        commands.append(VerificationCommand("go-test", ("go", "test", "./..."), 300))
    if (root / "CMakeLists.txt").exists():
        commands.extend([
            VerificationCommand("cmake-configure", ("cmake", "-S", ".", "-B", ".aicoder-build"), 240),
            VerificationCommand("cmake-build", ("cmake", "--build", ".aicoder-build", "-j2"), 300),
        ])
    if (root / "Makefile").exists() and not commands:
        commands.append(VerificationCommand("make", ("make", "-j2"), 300))

    # A repository with no known metadata still gets a content-level sanity gate.
    if not commands:
        commands.append(VerificationCommand("git-diff-check", ("git", "diff", "--check"), 60))
    return commands


def execute_verification_plan(root: str | Path, commands: Iterable[VerificationCommand]) -> list[VerificationResult]:
    root = Path(root)
    results: list[VerificationResult] = []
    for command in commands:
        started = time.monotonic()
        try:
            proc = subprocess.run(list(command.argv), cwd=str(root), capture_output=True, text=True, timeout=command.timeout)
            results.append(VerificationResult(
                command.name, list(command.argv), proc.returncode == 0, proc.returncode,
                int((time.monotonic() - started) * 1000), (proc.stdout + "\n" + proc.stderr)[-12000:], command.required,
            ))
        except (OSError, subprocess.SubprocessError) as exc:
            results.append(VerificationResult(
                command.name, list(command.argv), False, -1, int((time.monotonic() - started) * 1000),
                f"{type(exc).__name__}: {exc}", command.required,
            ))
    return results


def verification_passed(results: Iterable[VerificationResult]) -> bool:
    rows = list(results)
    return bool(rows) and all(row.ok for row in rows if row.required)


def content_fingerprint(diff_text: str) -> str:
    return hashlib.sha256(str(diff_text).encode("utf-8", errors="replace")).hexdigest()


def blind_candidate_id(diff_text: str = "") -> str:
    """Return a random model-neutral candidate run id.

    Content identity is tracked separately via ``content_fingerprint`` so two
    identical or empty diffs never collide at the filesystem/logging layer.
    """
    token = hashlib.sha256(uuid.uuid4().bytes).hexdigest()[:12]
    return "cand-" + token


def objective_rank_key(evaluation: dict[str, Any]) -> tuple:
    """Model/slot-independent ranking. Higher tuple wins; hash is deterministic tiebreak."""
    checks = evaluation.get("checks") or {}
    required = list(checks.values())
    passed = sum(1 for item in required if bool(item.get("ok")))
    failed = sum(1 for item in required if not bool(item.get("ok")))
    delta = evaluation.get("delta") or {}
    churn = int(delta.get("changed_count", 0)) + int(delta.get("deleted_count", 0))
    score = int(evaluation.get("score") or 0)
    fingerprint = content_fingerprint(str(evaluation.get("diff") or ""))
    # Prefer: zero failures, more passing gates, higher objective score, then less churn.
    # Final hash tie-break avoids slot/model order bias while remaining reproducible.
    return (-failed, passed, score, -churn, fingerprint)
