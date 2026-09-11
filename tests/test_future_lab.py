from aicoder.future_lab import _eligible_ai_endpoints,_memory_candidates,_round_prompt

def test_eligible_ai_requires_online_and_ai_chat():
    rows=[{"handle":"a","kind":"ai","online":True,"accept_ai_chat":True},{"handle":"b","kind":"ai","online":False,"accept_ai_chat":True},{"handle":"m","kind":"mcp","online":True,"accept_ai_chat":True}]
    assert [x["handle"] for x in _eligible_ai_endpoints(rows,[])]==["a"]

def test_round_two_contains_other_agents_and_challenge_instruction():
    text=_round_prompt("future",2,[{"sender":"@a","body":"idea A"},{"sender":"@b","body":"idea B"}],max_chars=5000)
    assert "@a: idea A" in text and "@b: idea B" in text and "challenge" in text

def test_memory_candidates_are_unverified_and_low_confidence():
    rows=_memory_candidates("future","fl_x",[{"sender":"@a","body":"possible idea"}])
    assert rows[0]["verification_state"]=="observation" and rows[0]["promoted"] is False and rows[0]["confidence"]<0.5

def test_final_round_is_synthesis_not_authority():
    text=_round_prompt("future",3,[{"sender":"@a","body":"idea"}],max_chars=5000)
    assert "CONSENSUS" in text and "hypotheses" in text and "no operator decisions" in text.lower()

def test_candidates_keep_provenance_and_never_promote():
    rows=_memory_candidates("topic","fl_1",[{"sender":"@model","body":"hypothesis"}])
    assert rows == [{"type":"hypothesis_bundle","verification_state":"observation","confidence":0.35,"topic":"topic","run_id":"fl_1","source":"@model","content":"hypothesis","promoted":False}]

def test_candidates_keep_provenance_and_never_promote():
    rows=_memory_candidates("topic","fl_1",[{"sender":"@model","body":"hypothesis"}])
    assert rows == [{"type":"hypothesis_bundle","verification_state":"observation","confidence":0.35,"topic":"topic","run_id":"fl_1","source":"@model","content":"hypothesis","promoted":False}]


def test_run_emits_conversation_immediately(monkeypatch, tmp_path):
    from aicoder import future_lab as fl
    from aicoder import shared_notify as shared
    monkeypatch.setattr(fl, "RUN_DIR", tmp_path / "runs")
    monkeypatch.setattr(shared, "load_shared_notify_state", lambda create_identity=False: shared.SharedNotifyState(enabled=True, endpoint_id="ep_me", handle="@me"))
    class Client:
        def notify_directory(self, include_offline=False):
            return {"endpoints":[{"handle":"@a","kind":"ai","online":True,"accept_ai_chat":True},{"handle":"@b","kind":"ai","online":True,"accept_ai_chat":True}]}
        def notify_conversation_create(self, title, endpoint_id, handles, kind="group"):
            return {"conversation":{"conversation_id":"conv_live","title":title,"kind":kind,"members":[{"handle":h} for h in handles]}}
        def notify_conversation_send(self, cid, payload): return {"deliveries":[]}
        def notify_conversation_history(self, cid, limit=500): return {"messages":[]}
    monkeypatch.setattr(shared, "_client", lambda: Client())
    monkeypatch.setattr(fl, "_collect_replies", lambda *a, **k: [])
    opened=[]
    run=fl.run_future_lab(fl.FutureLabConfig("topic", rounds=2, response_timeout=1), on_conversation=opened.append)
    assert opened[0]["conversation_id"] == "conv_live"
    assert opened[0]["future_lab"] is True
    assert run.status == "partial"
