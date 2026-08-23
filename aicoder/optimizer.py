"""Evidence-first system optimizer with a persistent safe plan lifecycle."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import uuid
from typing import Any

from .config import CONFIG_DIR, atomic_write_private
from .local_os import LocalOSToolProvider


@dataclass
class OptimizationPlan:
    id: str
    goal: str
    created_at: str
    priorities: dict[str, str]
    evidence: dict[str, Any]
    proposed_actions: list[dict[str, Any]]
    status: str = "planned"
    applied_actions: list[dict[str, Any]] = field(default_factory=list)
    verification_results: list[dict[str, Any]] = field(default_factory=list)
    rollback_change_ids: list[str] = field(default_factory=list)
    lifecycle_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OptimizationPlan":
        return cls(
            id=str(data.get("id") or ""), goal=str(data.get("goal") or ""),
            created_at=str(data.get("created_at") or ""), priorities=dict(data.get("priorities") or {}),
            evidence=dict(data.get("evidence") or {}), proposed_actions=list(data.get("proposed_actions") or []),
            status=str(data.get("status") or "planned"), applied_actions=list(data.get("applied_actions") or []),
            verification_results=list(data.get("verification_results") or []),
            rollback_change_ids=[str(x) for x in data.get("rollback_change_ids") or []],
            lifecycle_note=str(data.get("lifecycle_note") or ""),
        )


class OptimizationPlanStore:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else CONFIG_DIR / "optimizer" / "plans"

    def _ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        try: os.chmod(self.root, 0o700)
        except OSError: pass

    def _path(self, plan_id: str) -> Path:
        if not re.fullmatch(r"opt-[A-Za-z0-9T-]+", str(plan_id or "")):
            raise ValueError("invalid optimization plan id")
        self._ensure(); return self.root / f"{plan_id}.json"

    def save(self, plan: OptimizationPlan) -> OptimizationPlan:
        atomic_write_private(self._path(plan.id), json.dumps(plan.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        return plan

    def load(self, plan_id: str) -> OptimizationPlan | None:
        try:
            data=json.loads(self._path(plan_id).read_text(encoding="utf-8"))
            return OptimizationPlan.from_dict(data) if isinstance(data,dict) else None
        except (OSError, ValueError, TypeError):
            return None

    def list(self, limit: int = 50) -> list[OptimizationPlan]:
        self._ensure(); out=[]
        for path in sorted(self.root.glob("opt-*.json"), key=lambda p:p.stat().st_mtime_ns, reverse=True):
            try:
                data=json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data,dict): out.append(OptimizationPlan.from_dict(data))
            except (OSError,ValueError,TypeError): pass
            if len(out)>=max(1,min(200,int(limit))): break
        return out


def _decode(text: str) -> Any:
    try: return json.loads(text)
    except ValueError: return {"text": text}


def inspect_system(provider: LocalOSToolProvider | None = None) -> dict[str, Any]:
    p=provider or LocalOSToolProvider(); evidence={}
    for name,args in (("os_system_overview",{}),("os_kernel_info",{}),("os_storage_overview",{})):
        text,err=p.execute(name,args); evidence[name]={"ok":not err,"data":_decode(text)}
    return evidence


def _priorities(goal: str) -> dict[str,str]:
    text=(goal or "").lower(); result={"stability":"very_high"}
    mapping={"docker":"containers","container":"containers","python":"development","code":"development","local ai":"local_ai","llm":"local_ai","battery":"battery","akku":"battery","privacy":"privacy","server":"server","latency":"low_latency","latenz":"low_latency"}
    for signal,key in mapping.items():
        if signal in text: result[key]="high"
    return result


def build_plan(goal: str, provider: LocalOSToolProvider | None = None) -> OptimizationPlan:
    evidence=inspect_system(provider); actions=[]
    overview=evidence.get("os_system_overview",{}).get("data",{})
    memory=overview.get("memory",{}) if isinstance(overview,dict) else {}
    if memory:
        actions.append({
            "kind":"verify_memory_pressure","mutation":False,
            "tool":"os_system_overview","arguments":{},
            "evidence":f"MemAvailable={memory.get('MemAvailable','unknown')}",
            "reason":"Measure actual memory pressure before considering memory or swap tuning.",
            "verification":"Compare workload memory pressure and swap activity; no setting change is proposed yet.",
            "rollback":"not required: read-only diagnostic",
        })
    if re.search(r"docker|container",goal or "",re.I):
        actions.append({
            "kind":"inspect_containers","mutation":False,
            "tool":"os_container_list","arguments":{"runtime":"auto"},
            "evidence":"Goal explicitly prioritizes containers.",
            "reason":"Inspect running containers and resource use before changing Docker or kernel settings.",
            "verification":"Collect container list/stats and identify a measured bottleneck.",
            "rollback":"not required: read-only diagnostic",
        })
    actions.append({
        "kind":"baseline_first","mutation":False,
        "tool":"os_system_overview","arguments":{},
        "evidence":f"kernel={overview.get('release','unknown') if isinstance(overview,dict) else 'unknown'}",
        "reason":"No mutation is justified without a measured bottleneck and rollback target.",
        "verification":"Establish a workload-specific baseline before proposing any mutable action.",
        "rollback":"not required: read-only diagnostic",
    })
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return OptimizationPlan(
        f"opt-{stamp}-{uuid.uuid4().hex[:8]}",str(goal),datetime.now(timezone.utc).isoformat(),
        _priorities(goal),evidence,actions,
    )


def _execute_readonly_action(provider: LocalOSToolProvider, action: dict[str, Any]) -> dict[str, Any]:
    if bool(action.get("mutation")):
        return {
            "kind": str(action.get("kind") or "unknown"), "ok": False, "mutation": True,
            "error": "optimizer mutation refused: no explicit typed optimizer mutation handler is registered",
        }
    tool=str(action.get("tool") or "")
    allowed={"os_system_overview","os_kernel_info","os_storage_overview","os_container_list","os_package_list_upgradable"}
    if tool not in allowed:
        return {"kind":str(action.get("kind") or "unknown"),"ok":False,"mutation":False,"error":f"unsupported read-only optimizer action tool: {tool or '?'}"}
    args=action.get("arguments") if isinstance(action.get("arguments"),dict) else {}
    text,err=provider.execute(tool,args)
    return {"kind":str(action.get("kind") or tool),"tool":tool,"mutation":False,"ok":not err,"result":_decode(text)}


def apply_plan(plan_id: str, *, provider: LocalOSToolProvider | None = None, store: OptimizationPlanStore | None = None) -> OptimizationPlan:
    store=store or OptimizationPlanStore(); plan=store.load(plan_id)
    if plan is None: raise ValueError("optimization plan not found")
    if plan.status not in {"planned","failed"}: raise ValueError(f"optimization plan cannot be applied from status={plan.status}")
    p=provider or LocalOSToolProvider(); results=[]
    for action in plan.proposed_actions:
        result=_execute_readonly_action(p,action); results.append(result)
        if not result.get("ok"):
            plan.status="failed"; plan.applied_actions=results; plan.lifecycle_note=str(result.get("error") or "action failed"); store.save(plan); return plan
    plan.applied_actions=results; plan.status="applied"; plan.lifecycle_note="Plan applied; current actions were read-only diagnostics."; return store.save(plan)


def verify_plan(plan_id: str, *, provider: LocalOSToolProvider | None = None, store: OptimizationPlanStore | None = None) -> OptimizationPlan:
    store=store or OptimizationPlanStore(); plan=store.load(plan_id)
    if plan is None: raise ValueError("optimization plan not found")
    if plan.status not in {"applied","verified"}: raise ValueError(f"optimization plan cannot be verified from status={plan.status}")
    p=provider or LocalOSToolProvider(); results=[_execute_readonly_action(p,action) for action in plan.proposed_actions]
    plan.verification_results=results
    plan.status="verified" if all(item.get("ok") for item in results) else "failed"
    plan.lifecycle_note="Verification completed." if plan.status=="verified" else "Verification failed."
    return store.save(plan)


def rollback_plan(plan_id: str, *, store: OptimizationPlanStore | None = None) -> OptimizationPlan:
    store=store or OptimizationPlanStore(); plan=store.load(plan_id)
    if plan is None: raise ValueError("optimization plan not found")
    mutating=[item for item in plan.applied_actions if item.get("mutation")]
    if mutating:
        raise ValueError("optimizer plan contains mutations but no typed rollback linkage; refusing generic rollback")
    plan.status="rolled_back"
    plan.lifecycle_note="No state-changing optimizer actions were applied; rollback is a verified no-op."
    return store.save(plan)
