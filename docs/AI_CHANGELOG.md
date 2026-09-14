# AICoder AI Change Log

This file is the fallback implementation record when a change does not belong in a more specific authoritative document. It complements, rather than replaces, architecture/security/user documentation.

## 2026-09-14 — Inventory-aware progressive tool disclosure

- **Scope:** AICoder capability discovery and system-prompt operating policy.
- **Reason:** Reduce model context/token cost while keeping the full enabled tool catalogue discoverable on demand.
- **Changed subsystems:** `aicoder/capabilities.py`, `aicoder/agent_runtime.py`, `aicoder/executor.py`.
- **Behavior:** `toolbox_search(mode="inventories")` returns compact semantic inventories; normal `toolbox_search` returns short inactive-tool hints; `capability_request` can activate a named inventory, capability, or tool while existing tool-budget and expansion limits remain enforced.
- **Execution discipline:** prompts now require recovery backup before mutation, coherent architecture inspection, evidence-based debugging, focused tests/logs/reproducer, current primary documentation for version-sensitive claims, documentation updates, and approved feature-memory capture (Claude-Mem when configured through TriForce).
- **Verification:** focused capability/runtime/prompt/portable-device tests passed after the change.
- **Recovery:** `.workspacebackup/20260914-042212-inventory-prompt-optimization/backup.md`.
