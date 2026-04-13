from __future__ import annotations
import os
from pathlib import Path
from typing import Dict, List, Optional

# Canonical doc files that ai-coder understands
DOC_CANDIDATES: List[str] = [
    "AGENTS.md",
    "README.md",
    "CONTRIBUTING.md",
    "ARCHITECTURE.md",
    "docs/architecture.md",
    "docs/cli.md",
    "docs/models.md",
    "docs/swarm.md",
    "docs/security.md",
    "docs/backend_scope.md",
]

def find_project_root(start: Optional[str] = None) -> Path:
    """Walk up from start (or cwd) to find git root, else return cwd."""
    cur = Path(start or os.getcwd()).resolve()
    for p in [cur, *cur.parents]:
        if (p / ".git").exists():
            return p
    return cur

def collect_docs(project_root: Optional[str] = None) -> Dict[str, str]:
    """
    Return a dict of {relative_path: absolute_path} for all doc files
    that actually exist in the project. V2 scope: discovery only.
    """
    root = find_project_root(project_root)
    found: Dict[str, str] = {}
    for candidate in DOC_CANDIDATES:
        p = root / candidate
        if p.exists() and p.is_file():
            found[candidate] = str(p)
    return found

def context_summary(project_root: Optional[str] = None) -> Dict:
    """Structured summary for use in aicoder status."""
    root = find_project_root(project_root)
    docs = collect_docs(str(root))
    return {
        "project_root": str(root),
        "doc_files_found": len(docs),
        "docs": docs,
        "agents_md_present": "AGENTS.md" in docs,
    }

def read_agents_md(project_root: Optional[str] = None) -> Optional[str]:
    """
    Read AGENTS.md content if present. Used as system_prompt for LLM calls.
    Returns None if not found.
    """
    root = find_project_root(project_root)
    p = root / "AGENTS.md"
    if p.exists() and p.is_file():
        try:
            return p.read_text(encoding="utf-8").strip()
        except Exception:
            return None
    return None
