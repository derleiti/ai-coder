from __future__ import annotations
"""
swarm_runner.py — Lokaler Swarm V2 für ai-coder.

Wenn swarm_mode=on|review: Operator-Modell + Fallback-Modell parallel befragen,
Ergebnisse nebeneinander anzeigen. Operator bleibt primär.

V2-Scope: sequentiell (kein asyncio), 2 Modelle, keine Backend-Swarm-API.
V3: echte parallele Calls via threading + swarm_broadcast MCP.
"""
import sys
import threading
from typing import Optional

from .client import ClientError, TriForceClient
from .config import load_session
from .docs_context import read_agents_md
from .history import record as history_record
from .session_state import get_state
from .status import Spinner, phase_label


def _call(client: TriForceClient, message: str, model: Optional[str],
          system_prompt: Optional[str], result_box: list) -> None:
    """Thread target — puts (response_dict | exception) into result_box."""
    try:
        r = client.chat(message=message, model=model,
                        system_prompt=system_prompt,
                        temperature=0.7, max_tokens=4096)
        result_box.append(r)
    except Exception as e:
        result_box.append(e)


def run_swarm_ask(
    message: str,
    operator_model: Optional[str],
    fallback_model: Optional[str],
    system_prompt: Optional[str],
    mode: str,          # "on" | "review"
    client: Optional[TriForceClient] = None,
) -> int:
    """Run the operator first-class and the fallback only as an advisor.

    ``on`` may ask both models concurrently. ``review`` is necessarily
    sequential because the advisor reviews the actual operator response.
    """
    if client is None:
        session = load_session()
        client  = TriForceClient(session.base_url, token=session.token)

    op_box: list = []
    fb_box: list = []

    label = "swarming..." if mode != "review" else "reviewing..."

    with Spinner(label):
        if mode == "review":
            _call(client, message, operator_model, system_prompt, op_box)
            op_result = op_box[0] if op_box else None
            if fallback_model and isinstance(op_result, dict):
                review_prompt = (
                    "Act only as an advisor. Review the operator response for factual "
                    "errors, security risks, missing verification, and conflicts with the "
                    "original request. Do not execute tools.\n\n"
                    f"Original request:\n{message[:4000]}\n\n"
                    f"Operator response:\n{op_result.get('response', '')[:12000]}"
                )
                _call(client, review_prompt, fallback_model, system_prompt, fb_box)
        elif fallback_model and fallback_model != operator_model:
            t1 = threading.Thread(
                target=_call,
                args=(client, message, operator_model, system_prompt, op_box),
                daemon=True,
            )
            t2 = threading.Thread(
                target=_call,
                args=(client, message, fallback_model, system_prompt, fb_box),
                daemon=True,
            )
            t1.start()
            t2.start()
            # Network calls own their timeout; wait for completion so no hidden
            # background request survives after this command returns.
            t1.join()
            t2.join()
        else:
            _call(client, message, operator_model, system_prompt, op_box)

    # Operator result
    op = op_box[0] if op_box else None
    fb = fb_box[0] if fb_box else None

    print()
    print("── Operator " + "─" * 38)
    if isinstance(op, Exception):
        print(f"  Fehler: {op}", file=sys.stderr)
    elif op:
        print(op.get("response", ""))
        lat = op.get("latency_ms")
        print(f"\n[{op.get('model','?')} · {lat or '?'}ms]", file=sys.stderr)
        try:
            history_record(kind="ask", prompt=message,
                           response=op.get("response",""),
                           model=op.get("model"), latency_ms=lat)
        except Exception:
            pass
    else:
        print("  (Timeout)", file=sys.stderr)

    print()
    print("── " + ("Swarm Review " if mode == "review" else "Swarm Advisor ") + "─" * 32)
    if isinstance(fb, Exception):
        print(f"  Fehler: {fb}", file=sys.stderr)
    elif fb:
        print(fb.get("response", ""))
        lat2 = fb.get("latency_ms")
        print(f"\n[{fb.get('model','?')} · {lat2 or '?'}ms]", file=sys.stderr)
    elif not fallback_model:
        print("  (kein Fallback-Modell gesetzt — swarm benötigt fallback)", file=sys.stderr)
    else:
        print("  (kein Advisor-Ergebnis)", file=sys.stderr)

    print()
    return 1 if isinstance(op, Exception) or op is None else 0


# ---------------------------------------------------------------------------
# Auto-Swarm Heuristik
# ---------------------------------------------------------------------------

_AUTO_KEYWORDS = {
    "refactor", "design", "architect", "strategy", "compare", "analyse",
    "analyze", "review", "tradeoff", "trade-off", "alternative", "approach",
    "best practice", "optimize", "optimise", "security", "risk", "migrate",
    "migration", "restructure", "rewrite",
}


def should_auto_swarm(message: str) -> bool:
    """
    Heuristik: Swarm bei komplexen Tasks automatisch aktivieren.
    Trigger: Prompt >300 Zeichen ODER enthält Komplexitäts-Keywords.
    """
    lower = message.lower()
    if len(message) > 300:
        return True
    return any(kw in lower for kw in _AUTO_KEYWORDS)


# ---------------------------------------------------------------------------
# Task Review via Swarm (nach LLM-Output)
# ---------------------------------------------------------------------------

def run_swarm_review(
    original_task: str,
    operator_response: str,
    operator_model: Optional[str],
    fallback_model: Optional[str],
    system_prompt: Optional[str],
    client: Optional[TriForceClient] = None,
) -> None:
    """
    Schickt den Operator-Output als Review-Prompt ans Fallback-Modell.
    Gibt das Review auf stderr aus (non-blocking: ignored on error).
    """
    if not fallback_model or fallback_model == operator_model:
        return

    review_prompt = (
        f"Review the following code/solution for bugs, risks, and improvements. "
        f"Be concise. Original task: {original_task[:200]}\n\n"
        f"Solution to review:\n{operator_response[:3000]}"
    )

    if client is None:
        session = load_session()
        client  = TriForceClient(session.base_url, token=session.token)
    box: list = []

    try:
        _call(client, review_prompt, fallback_model, system_prompt, box)
    except Exception:
        return

    if box and not isinstance(box[0], Exception):
        review = box[0].get("response", "").strip()
        if review:
            print("\n── Swarm Review (" + (fallback_model or "?") + ") " + "─" * 20, file=sys.stderr)
            print(review, file=sys.stderr)
            print("─" * 50, file=sys.stderr)
