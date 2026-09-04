"""Deterministic stage gates and project-aware verification for team runs."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
from typing import Any, Iterable


class TeamStage(str, Enum):
    PLAN_RESEARCH = "plan_research"
    RESEARCH = "research"
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


def configured_project_python(root: str | Path) -> str | None:
    """Return an explicitly configured or project-local Python test interpreter.

    ``AICODER_TEST_PYTHON`` is intentionally process-scoped so a CI/dev runner
    can provide the dependency-complete interpreter without persisting host paths
    into project state. Project-local virtual environments remain automatic.
    """
    root = Path(root).expanduser().resolve(strict=False)
    candidates: list[Path] = []
    override = str(os.environ.get("AICODER_TEST_PYTHON") or "").strip()
    if override:
        candidates.append(Path(override).expanduser())
    if os.name == "nt":
        candidates.extend([root / ".venv" / "Scripts" / "python.exe", root / "venv" / "Scripts" / "python.exe"])
    else:
        candidates.extend([root / ".venv" / "bin" / "python", root / "venv" / "bin" / "python"])
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return str(resolved)
    return None


def project_python_interpreter(root: str | Path) -> str:
    """Interpreter used by deterministic Python project checks."""
    return configured_project_python(root) or "python3"


def normalize_project_test_argv(argv: list[str], root: str | Path) -> list[str]:
    """Route Python test commands through the configured project interpreter.

    Non-Python test runners and unconfigured environments are preserved exactly.
    """
    configured = configured_project_python(root)
    if not configured or not argv:
        return list(argv)
    executable = Path(argv[0]).name.lower()
    if executable in {"pytest", "py.test"}:
        return [configured, "-m", "pytest", *argv[1:]]
    if executable in {"python", "python3", "python.exe"} and len(argv) >= 3 and argv[1] == "-m" and argv[2] in {"pytest", "unittest"}:
        return [configured, *argv[1:]]
    return list(argv)


def project_verification_plan(root: str | Path) -> list[VerificationCommand]:
    """Infer deterministic checks from repository-native metadata, without an LLM vote."""
    root = Path(root)
    commands: list[VerificationCommand] = []
    python = project_python_interpreter(root)

    if (root / "pyproject.toml").exists() or (root / "setup.py").exists() or (root / "setup.cfg").exists():
        commands.append(VerificationCommand("python-compile", (python, "-m", "compileall", "-q", "."), 120))
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
                commands.append(VerificationCommand("python-tests", (python, "-m", "pytest", "-q"), 300))
            else:
                commands.append(VerificationCommand("python-tests", (python, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"), 300))
        if (root / "ruff.toml").exists() or (root / ".ruff.toml").exists():
            commands.append(VerificationCommand("ruff", (python, "-m", "ruff", "check", "."), 180))
        if (root / "mypy.ini").exists() or (root / ".mypy.ini").exists():
            commands.append(VerificationCommand("mypy", (python, "-m", "mypy", "."), 240))

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
