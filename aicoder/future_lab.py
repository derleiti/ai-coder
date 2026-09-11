"""Structured multi-AI Future Lab over AILinux Shared Notify.

Advisory only: transcripts and unverified memory candidates are created here;
operator decisions and curated facts are never created automatically.
"""
from __future__ import annotations
import json, secrets, time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from .config import CONFIG_DIR, atomic_write_private
from . import shared_notify as shared

RUN_DIR = CONFIG_DIR / "future_lab" / "runs"

@dataclass
class FutureLabConfig:
    topic: str
    participants: list[str] = field(default_factory=list)
    rounds: int = 3
    response_timeout: float = 120.0
    poll_interval: float = 2.0
    max_context_chars: int = 12000
    include_smalltalk: bool = True

@dataclass
class FutureLabRun:
    run_id: str; topic: str; conversation_id: str; participants: list[str]
    rounds: list[dict[str, Any]]; memory_candidates: list[dict[str, Any]]
    status: str; created_at: int; completed_at: int | None = None

def _handle(v: str) -> str:
    v = str(v or "").strip(); return ("@" + v.lstrip("@")) if v else ""

def _eligible_ai_endpoints(directory, requested):
    wanted = {_handle(x).lower() for x in requested if _handle(x)}
    rows = []
    for row in directory:
        h = _handle(row.get("handle", ""))
        if str(row.get("kind", "")).lower() != "ai" or not h: continue
        if wanted and h.lower() not in wanted: continue
        if not row.get("online") or not row.get("accept_ai_chat"): continue
        rows.append(dict(row))
    return sorted(rows, key=lambda r: _handle(r.get("handle", "")).lower())

def _round_prompt(topic, n, previous, *, include_smalltalk=True, max_chars=12000):
    if n == 1:
        warm = "Begin with one brief natural observation about the project. " if include_smalltalk else ""
        return ("You are in the AILinux Future Lab. Advisory brainstorming only: assumptions are not facts; execute no tools/actions. "
                + warm + f"\nTOPIC: {topic}\nGive an independent view: opportunities, user value, prerequisites, risks, and one bold idea. Be concise.")
    transcript = "\n\n".join(f"{x.get('sender','AI')}: {x.get('body','')}" for x in previous)[-max_chars:]
    instruction = ("React to the other participants: agree where warranted, challenge one weak assumption, combine compatible ideas, and propose a low-risk experiment."
                   if n == 2 else
                   "Final synthesis. Use sections CONSENSUS, RISKS, EXPERIMENTS, OPEN_QUESTIONS. Label speculation as hypotheses. Make no operator decisions.")
    return f"You are in the AILinux Future Lab. Advisory brainstorming only; execute no tools/actions.\nTOPIC: {topic}\nROUND: {n}\n\nPREVIOUS ROUND:\n{transcript}\n\n{instruction}"

def _collect_replies(client, conversation_id, delivery_ids, expected, *, timeout, poll_interval):
    deadline=time.monotonic()+max(1.0,float(timeout)); replies={}
    while time.monotonic()<deadline:
        try: shared.poll_once(dispatch_ai=True)
        except Exception: pass
        for msg in client.notify_conversation_history(conversation_id, limit=500).get("messages") or []:
            meta=msg.get("metadata") if isinstance(msg.get("metadata"),dict) else {}
            sender=_handle(msg.get("sender_handle", ""))
            if str(meta.get("in_reply_to", "")) in delivery_ids and sender.lower() in expected:
                replies[sender.lower()]={"sender":sender,"body":str(msg.get("body") or ""),"message_id":str(msg.get("message_id") or ""),"in_reply_to":str(meta.get("in_reply_to") or "")}
        if len(replies)>=len(expected): break
        time.sleep(max(.25,min(float(poll_interval),10.0)))
    return [replies[k] for k in sorted(replies)]

def _memory_candidates(topic, run_id, replies):
    return [{"type":"hypothesis_bundle","verification_state":"observation","confidence":.35,"topic":topic,"run_id":run_id,"source":r.get("sender"),"content":str(r.get("body") or "")[:6000],"promoted":False} for r in replies if str(r.get("body") or "").strip()]

def save_run(run):
    RUN_DIR.mkdir(parents=True,exist_ok=True); path=RUN_DIR/f"{run.run_id}.json"
    atomic_write_private(path,json.dumps(asdict(run),indent=2,ensure_ascii=False)+"\n"); return path

def run_future_lab(config: FutureLabConfig):
    topic=str(config.topic or "").strip()
    if not topic: raise ValueError("Future Lab topic is required")
    rounds=max(2,min(int(config.rounds or 3),6)); state=shared.load_shared_notify_state(create_identity=False)
    if not state.enabled or not state.endpoint_id: raise RuntimeError("Shared Notify must be enabled")
    client=shared._client(); directory=client.notify_directory(include_offline=False).get("endpoints") or []
    participants=_eligible_ai_endpoints(directory,config.participants)
    if len(participants)<2: raise RuntimeError("Future Lab requires at least two online AI endpoints accepting AI chat")
    handles=[_handle(r.get("handle","")) for r in participants]
    created=client.notify_conversation_create(f"Future Lab: {topic[:80]}",state.endpoint_id,handles,kind="group")
    cid=str((created.get("conversation") or {}).get("conversation_id") or "")
    if not cid: raise RuntimeError("Future Lab conversation creation returned no conversation_id")
    run=FutureLabRun("fl_"+secrets.token_urlsafe(10).replace("-","_"),topic,cid,handles,[],[],"running",int(time.time())); save_run(run)
    previous=[]; expected={h.lower() for h in handles}
    for n in range(1,rounds+1):
        prompt=_round_prompt(topic,n,previous,include_smalltalk=config.include_smalltalk,max_chars=max(2000,int(config.max_context_chars)))
        sent=client.notify_conversation_send(cid,{"sender_endpoint_id":state.endpoint_id,"kind":"brainstorm","title":f"Future Lab round {n}/{rounds}","body":prompt,"metadata":{"expect_reply":True,"future_lab":True,"future_lab_run_id":run.run_id,"future_lab_round":n},"ttl_seconds":max(60,min(int(config.response_timeout*3),3600))})
        ids={str(x.get("message_id") or "") for x in sent.get("deliveries") or [] if x.get("message_id")}
        replies=_collect_replies(client,cid,ids,expected,timeout=config.response_timeout,poll_interval=config.poll_interval)
        run.rounds.append({"round":n,"prompt":prompt,"delivery_ids":sorted(ids),"replies":replies,"complete":len(replies)==len(expected)})
        if replies:
            previous = replies
        save_run(run)
    # A slow participant may answer after its round deadline. Recover any late
    # replies before distillation so useful thought is not discarded merely
    # because a provider was temporarily slow.
    history = client.notify_conversation_history(cid, limit=500).get("messages") or []
    by_parent = {}
    for msg in history:
        meta = msg.get("metadata") if isinstance(msg.get("metadata"), dict) else {}
        parent = str(meta.get("in_reply_to") or "")
        sender = _handle(msg.get("sender_handle", ""))
        if parent and sender.lower() in expected:
            by_parent[(parent, sender.lower())] = {"sender": sender, "body": str(msg.get("body") or ""), "message_id": str(msg.get("message_id") or ""), "in_reply_to": parent}
    for round_row in run.rounds:
        known = {str(x.get("sender") or "").lower() for x in round_row["replies"]}
        for parent in round_row["delivery_ids"]:
            for sender in expected - known:
                reply = by_parent.get((parent, sender))
                if reply:
                    round_row["replies"].append(reply); known.add(sender)
        round_row["replies"].sort(key=lambda x: str(x.get("sender") or "").lower())
        round_row["complete"] = len(known) == len(expected)
    final=list(run.rounds[-1]["replies"]) if run.rounds else []
    if not final:
        # Distillation may still preserve the newest useful round when the final
        # provider round timed out completely. It remains explicitly unverified.
        for round_row in reversed(run.rounds):
            if round_row["replies"]:
                final=list(round_row["replies"]); break
    run.memory_candidates=_memory_candidates(topic,run.run_id,final)
    run.status = "completed" if run.rounds and all(r.get("complete") for r in run.rounds) else "partial"
    run.completed_at=int(time.time()); save_run(run); return run
