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
