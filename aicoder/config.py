from __future__ import annotations
import json, os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

APP_NAME = "ai-coder"
CONFIG_DIR = Path.home() / f".config/{APP_NAME}"
SESSION_FILE = CONFIG_DIR / "session.json"
DEFAULT_BASE_URL = os.environ.get("AILINUX_BASE_URL", "https://api.ailinux.me")

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
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps(session.to_dict(), indent=2), encoding="utf-8")
    os.chmod(SESSION_FILE, 0o600)

def load_session() -> Session:
    if not SESSION_FILE.exists():
        raise RuntimeError(f"Keine Session gefunden: {SESSION_FILE}")
    data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    return Session.from_dict(data)

def delete_session() -> None:
    if SESSION_FILE.exists():
        SESSION_FILE.unlink()
