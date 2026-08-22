"""Private structured records for state-changing AICoder actions."""
from __future__ import annotations
import json, os, re, uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from .config import CONFIG_DIR, atomic_write_private

_SECRET=re.compile(r"password|token|secret|api[_-]?key|authorization|cookie",re.I)
_ID=re.compile(r"^[A-Za-z0-9_.-]+$")
_INLINE=re.compile(r"(?i)\b(password|passwd|token|bearer|secret|api[_-]?key|authorization)\b(\s*[:=]\s*|\s+)([^\s,;]+)")

def _text(value: Any, limit: int=1000) -> str:
    raw=str(value or "")
    return _INLINE.sub(lambda m:f"{m.group(1)}{m.group(2)}[REDACTED]",raw)[:limit]

def _clean(v: Any, depth: int=0) -> Any:
    if depth>4: return "[TRUNCATED]"
    if v is None or isinstance(v,(bool,int,float)): return v
    if isinstance(v,str): return _text(v)
    if isinstance(v,list): return [_clean(x,depth+1) for x in v[:50]]
    if isinstance(v,dict): return {str(k)[:100]:("[REDACTED]" if _SECRET.search(str(k)) else _clean(x,depth+1)) for k,x in list(v.items())[:60]}
    return str(v)[:1000]

class ChangeJournal:
    def __init__(self, root: Path|None=None): self.root=Path(root) if root else CONFIG_DIR/"changes"
    def _ensure(self):
        self.root.mkdir(parents=True,exist_ok=True)
        try: os.chmod(self.root,0o700)
        except OSError: pass
    def _path(self, ident: str) -> Path:
        if not _ID.fullmatch(ident): raise ValueError("invalid change id")
        self._ensure(); return self.root/f"change-{ident}.json"
    def record(self, *, tool: str, arguments: dict[str,Any], risk: str, approved: bool, result: str, is_error: bool, reason: str="", reversible: dict[str,Any]|None=None) -> dict[str,Any]:
        stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        ident=f"{stamp}-{uuid.uuid4().hex[:8]}"
        data={"id":ident,"timestamp":datetime.now(timezone.utc).isoformat(),"tool":str(tool),"arguments":_clean(arguments),"risk":str(risk),"approved":bool(approved),"result_summary":_text(result,2000),"is_error":bool(is_error),"verification":"failed" if is_error else "pending","reversible":bool(reversible),"restore_metadata":_clean(reversible or {}),"reason":_text(reason,1000)}
        atomic_write_private(self._path(ident),json.dumps(data,ensure_ascii=False,indent=2)); return data
    def get(self, ident: str) -> dict[str,Any]|None:
        try:
            data=json.loads(self._path(ident).read_text()); return data if isinstance(data,dict) else None
        except (OSError,ValueError,TypeError): return None
    def list(self, limit: int=50) -> list[dict[str,Any]]:
        self._ensure(); out=[]
        for path in sorted(self.root.glob("change-*.json"),key=lambda p:p.stat().st_mtime_ns,reverse=True):
            try:
                data=json.loads(path.read_text())
                if isinstance(data,dict): out.append(data)
            except (OSError,ValueError,TypeError): pass
            if len(out)>=max(1,min(200,int(limit))): break
        return out
    def mark_verified(self, ident: str, status: str) -> bool:
        data=self.get(ident)
        if data is None: return False
        data["verification"]=str(status)[:80]
        atomic_write_private(self._path(ident),json.dumps(data,ensure_ascii=False,indent=2)); return True
