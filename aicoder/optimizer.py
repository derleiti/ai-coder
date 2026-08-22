"""Evidence-first system optimizer inspection and planning foundation."""
from __future__ import annotations
import json, re, uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any
from .local_os import LocalOSToolProvider

@dataclass
class OptimizationPlan:
    id: str
    goal: str
    created_at: str
    priorities: dict[str,str]
    evidence: dict[str,Any]
    proposed_actions: list[dict[str,Any]]
    def to_dict(self): return asdict(self)

def inspect_system(provider: LocalOSToolProvider|None=None) -> dict[str,Any]:
    p=provider or LocalOSToolProvider(); evidence={}
    for name,args in (("os_system_overview",{}),("os_kernel_info",{}),("os_storage_overview",{})):
        text,err=p.execute(name,args)
        try: value=json.loads(text)
        except ValueError: value={"text":text}
        evidence[name]={"ok":not err,"data":value}
    return evidence

def _priorities(goal: str) -> dict[str,str]:
    text=(goal or "").lower(); result={"stability":"very_high"}
    mapping={"docker":"containers","container":"containers","python":"development","code":"development","local ai":"local_ai","llm":"local_ai","battery":"battery","akku":"battery","privacy":"privacy","server":"server","latency":"low_latency","latenz":"low_latency"}
    for signal,key in mapping.items():
        if signal in text: result[key]="high"
    return result

def build_plan(goal: str, provider: LocalOSToolProvider|None=None) -> OptimizationPlan:
    evidence=inspect_system(provider); actions=[]
    overview=evidence.get("os_system_overview",{}).get("data",{})
    memory=overview.get("memory",{}) if isinstance(overview,dict) else {}
    if memory:
        actions.append({"kind":"verify_memory_pressure","mutation":False,"evidence":f"MemAvailable={memory.get('MemAvailable','unknown')}","reason":"Measure actual memory pressure before considering memory or swap tuning.","verification":"Compare workload memory pressure and swap activity; no setting change is proposed yet."})
    if re.search(r"docker|container",goal or "",re.I):
        actions.append({"kind":"inspect_containers","mutation":False,"evidence":"Goal explicitly prioritizes containers.","reason":"Inspect running containers and resource use before changing Docker or kernel settings.","verification":"Collect container list/stats and identify a measured bottleneck."})
    actions.append({"kind":"baseline_first","mutation":False,"evidence":f"kernel={overview.get('release','unknown') if isinstance(overview,dict) else 'unknown'}","reason":"No mutation is justified without a measured bottleneck and rollback target.","verification":"Establish a workload-specific baseline before proposing any mutable action."})
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return OptimizationPlan(f"opt-{stamp}-{uuid.uuid4().hex[:8]}",str(goal),datetime.now(timezone.utc).isoformat(),_priorities(goal),evidence,actions)
