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
import time
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
    """
    Run message through operator + fallback in parallel threads.
    Display both results. Operator output always first.
    """
    if client is None:
        session = load_session()
        client  = TriForceClient(session.base_url, token=session.token)

    op_box  = []
    fb_box  = []

    label = "swarming..." if mode != "review" else "hiveing..."

    with Spinner(label):
        t0 = time.time()
        t1 = threading.Thread(target=_call, args=(client, message, operator_model,  system_prompt, op_box),  daemon=True)
        t2 = threading.Thread(target=_call, args=(client, message, fallback_model, system_prompt, fb_box), daemon=True)
        t1.start()
        t2.start()
        t1.join(timeout=90)
        remaining = max(1, 90 - (time.time() - t0))
        t2.join(timeout=remaining)

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
    print("── Swarm/Fallback " + "─" * 32)
    if isinstance(fb, Exception):
        print(f"  Fehler: {fb}", file=sys.stderr)
    elif fb:
        print(fb.get("response", ""))
        lat2 = fb.get("latency_ms")
        print(f"\n[{fb.get('model','?')} · {lat2 or '?'}ms]", file=sys.stderr)
    else:
        print("  (kein Fallback-Modell gesetzt — swarm benötigt fallback)", file=sys.stderr)

    print()
    return 0


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
    if not fallback_model:
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
        t = threading.Thread(
            target=_call,
            args=(client, review_prompt, fallback_model, system_prompt, box),
            daemon=True,
        )
        t.start()
        t.join(timeout=60)
    except Exception:
        return

    if box and not isinstance(box[0], Exception):
        review = box[0].get("response", "").strip()
        if review:
            print("\n── Swarm Review (" + (fallback_model or "?") + ") " + "─" * 20, file=sys.stderr)
            print(review, file=sys.stderr)
            print("─" * 50, file=sys.stderr)
