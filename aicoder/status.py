from __future__ import annotations

import sys

from .ui import AgentSpinner, C

class Spinner(AgentSpinner):
    """Compatibility wrapper around the cursor-safe terminal spinner."""
    def __init__(self, text: str, file=None):
        super().__init__(text, color=C.CYAN, file=file or sys.stderr)

def phase_label(mode: str) -> str:
    mode = (mode or "").strip().lower()
    if mode in {"swarm", "swarming"}:
        return "swarming..."
    if mode in {"hive", "hivemind", "hiveing"}:
        return "hiveing..."
    return "working..."
