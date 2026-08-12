from __future__ import annotations
import json, os, tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

APP_NAME = "ai-coder"
CONFIG_DIR = Path.home() / f".config/{APP_NAME}"
SESSION_FILE = CONFIG_DIR / "session.json"
DEFAULT_BASE_URL = os.environ.get("AILINUX_BASE_URL", "https://api.ailinux.me")


def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass


def atomic_write_private(path: Path, text: str) -> None:
    """Atomically replace a UTF-8 config file with owner-only permissions."""
    ensure_config_dir()
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass

@dataclass
class Session:
    base_url: str
    token: str
    client_id: str
    user_id: str
    tier: str
    account_role: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "base_url": self.base_url,
            "token": self.token,
            "client_id": self.client_id,
            "user_id": self.user_id,
            "tier": self.tier,
            "account_role": self.account_role,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Session":
        return cls(
            base_url=data["base_url"],
            token=data["token"],
            client_id=data.get("client_id", ""),
            user_id=data.get("user_id", ""),
            tier=data.get("tier", "unknown"),
            account_role=data.get("account_role", "unknown"),
        )

    def masked(self) -> Dict[str, Any]:
        tok = self.token
        masked_token = tok[:10] + "..." + tok[-6:] if len(tok) > 20 else "***"
        return {
            "base_url": self.base_url,
            "client_id": self.client_id,
            "user_id": self.user_id,
            "tier": self.tier,
            "account_role": self.account_role,
            "token": masked_token,
        }

def save_session(session: Session) -> None:
    atomic_write_private(SESSION_FILE, json.dumps(session.to_dict(), indent=2))

def load_session() -> Session:
    if not SESSION_FILE.exists():
        raise RuntimeError(f"Keine Session gefunden: {SESSION_FILE}")
    data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    return Session.from_dict(data)

def delete_session() -> None:
    if SESSION_FILE.exists():
        SESSION_FILE.unlink()
