from __future__ import annotations
import json, os
from pathlib import Path
from typing import Any, Dict, Optional

CONFIG_DIR = Path.home() / ".config/ai-coder"
STATE_FILE = CONFIG_DIR / "state.json"

SWARM_MODES = {"off", "auto", "on", "review"}

_DEFAULTS: Dict[str, Any] = {
    "selected_model": None,
    "fallback_model": None,
    "swarm_mode": "off",
    "workspace_root": None,
}

def _load_raw() -> Dict[str, Any]:
    if not STATE_FILE.exists():
        return dict(_DEFAULTS)
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return {**_DEFAULTS, **data}
    except Exception:
        return dict(_DEFAULTS)

def _save_raw(data: Dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(STATE_FILE, 0o600)

def get_state() -> Dict[str, Any]:
    return _load_raw()

def set_model(model: str) -> None:
    d = _load_raw()
    d["selected_model"] = model
    _save_raw(d)

def set_fallback(model: str) -> None:
    d = _load_raw()
    d["fallback_model"] = model
    _save_raw(d)

def set_swarm(mode: str) -> None:
    if mode not in SWARM_MODES:
        raise ValueError(f"Ungültiger Swarm-Modus '{mode}'. Erlaubt: {', '.join(sorted(SWARM_MODES))}")
    d = _load_raw()
    d["swarm_mode"] = mode
    _save_raw(d)

def set_workspace(path: Optional[str]) -> None:
    d = _load_raw()
    d["workspace_root"] = path
    _save_raw(d)
