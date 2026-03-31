from __future__ import annotations
"""
ui.py — Terminal UI primitives for ai-coder.
opencode-inspired design: Braille spinner, box drawing, ANSI colors, panels.
"""
import itertools
import json
import os
import shutil
import sys
import threading
import time
from typing import Any

# ── Terminal-Breite ──────────────────────────────────────────────────────────

def term_width() -> int:
    return shutil.get_terminal_size((80, 24)).columns

# ── ANSI-Farben ──────────────────────────────────────────────────────────────

class C:
    RESET    = "\033[0m"
    BOLD     = "\033[1m"
    DIM      = "\033[2m"
    ITALIC   = "\033[3m"
    UL       = "\033[4m"
    # Foreground
    BLACK    = "\033[30m"
    RED      = "\033[31m"
    GREEN    = "\033[32m"
    YELLOW   = "\033[33m"
    BLUE     = "\033[34m"
    MAGENTA  = "\033[35m"
    CYAN     = "\033[36m"
    WHITE    = "\033[37m"
    # Bright
    BRED     = "\033[91m"
    BGREEN   = "\033[92m"
    BYELLOW  = "\033[93m"
    BBLUE    = "\033[94m"
    BMAGENTA = "\033[95m"
    BCYAN    = "\033[96m"
    BWHITE   = "\033[97m"
    # Background
    BG_BLACK  = "\033[40m"
    BG_CYAN   = "\033[46m"
    BG_BLUE   = "\033[44m"

def c(color: str, text: str) -> str:
    return color + text + C.RESET

def bold(t: str)    -> str: return c(C.BOLD, t)
def dim(t: str)     -> str: return c(C.DIM, t)
def cyan(t: str)    -> str: return c(C.CYAN, t)
def green(t: str)   -> str: return c(C.BGREEN, t)
def yellow(t: str)  -> str: return c(C.BYELLOW, t)
def red(t: str)     -> str: return c(C.BRED, t)
def magenta(t: str) -> str: return c(C.BMAGENTA, t)
def blue(t: str)    -> str: return c(C.BBLUE, t)
def white(t: str)   -> str: return c(C.BWHITE, t)

# ── Braille-Spinner ──────────────────────────────────────────────────────────

BRAILLE_CYCLE   = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
DOTS_CYCLE      = "⣾⣽⣻⢿⡿⣟⣯⣷"
ARROW_CYCLE     = "←↖↑↗→↘↓↙"
PULSE_CYCLE     = "▁▂▃▄▅▆▇█▇▆▅▄▃▂▁"

# Tool-Typ → Spinner-Style + Farbe
TOOL_STYLE: dict[str, tuple[str, str]] = {
    "code_read":    (BRAILLE_CYCLE, C.CYAN),
    "code_search":  (BRAILLE_CYCLE, C.CYAN),
    "code_tree":    (BRAILLE_CYCLE, C.CYAN),
    "code_edit":    (PULSE_CYCLE,   C.BYELLOW),
    "code_patch":   (PULSE_CYCLE,   C.BYELLOW),
    "shell":        (DOTS_CYCLE,    C.BGREEN),
    "binary_exec":  (DOTS_CYCLE,    C.BGREEN),
    "safe_probe":   (ARROW_CYCLE,   C.BBLUE),
    "system_info":  (ARROW_CYCLE,   C.BBLUE),
    "git":          (BRAILLE_CYCLE, C.BMAGENTA),
    "git_ops":      (BRAILLE_CYCLE, C.BMAGENTA),
    "web_search":   (DOTS_CYCLE,    C.BCYAN),
    "fetch":        (DOTS_CYCLE,    C.BCYAN),
    "dev_analyze":  (PULSE_CYCLE,   C.BYELLOW),
    "dev_debug":    (PULSE_CYCLE,   C.BRED),
    "dev_lint":     (BRAILLE_CYCLE, C.BYELLOW),
    "memory_store": (BRAILLE_CYCLE, C.BMAGENTA),
}

def _spinner_for(tool_name: str) -> tuple[str, str]:
    return TOOL_STYLE.get(tool_name, (BRAILLE_CYCLE, C.CYAN))


class AgentSpinner:
    """Styled spinner running on stderr."""
    def __init__(self, label: str, tool: str = "", color: str = C.CYAN):
        self.label = label
        self.color = color
        cycle, col = _spinner_for(tool) if tool else (BRAILLE_CYCLE, color)
        self._cycle = cycle
        self._col   = col
        self._stop  = threading.Event()
        self._t     = None
        self._start = time.time()

    def __enter__(self):
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        if self._t:
            self._t.join(timeout=1)
        elapsed = time.time() - self._start
        sys.stderr.write(f"\r{' ' * (term_width()-1)}\r")
        sys.stderr.flush()
        return elapsed

    def _run(self):
        for ch in itertools.cycle(self._cycle):
            if self._stop.is_set():
                break
            elapsed = time.time() - self._start
            line = (
                f"\r  {self._col}{ch}{C.RESET} "
                f"{dim(self.label)} "
                f"{C.DIM}{elapsed:.1f}s{C.RESET}"
            )
            sys.stderr.write(line)
            sys.stderr.flush()
            time.sleep(0.08)


# ── Box-Drawing Panels ───────────────────────────────────────────────────────

BOX = {
    "tl":"╭","tr":"╮","bl":"╰","br":"╯",
    "h":"─","v":"│","lt":"├","rt":"┤",
    "ttl":"┌","ttr":"┐","tbl":"└","tbr":"┘",
}

def _pad(s: str, width: int) -> str:
    """Pad string to width, aware of ANSI escape codes."""
    import re
    visible = re.sub(r'\033\[[0-9;]*m', '', s)
    pad = max(0, width - len(visible))
    return s + " " * pad


def panel(
    content: str,
    title: str = "",
    title_color: str = C.CYAN,
    border_color: str = C.DIM,
    max_lines: int = 50,
    indent: int = 2,
) -> str:
    w = min(term_width() - indent, 100)
    inner = w - 4  # padding 1 each side + border

    lines = content.splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [dim(f"… +{len(lines)-max_lines} more lines")]

    bc = border_color
    out = []
    # Top border
    if title:
        t_str = f" {title_color}{bold(title)}{C.RESET} "
        import re
        t_vis = len(re.sub(r'\033\[[0-9;]*m', '', t_str))
        fill = max(0, inner - t_vis + 2)
        out.append(
            " " * indent +
            bc + BOX["tl"] + BOX["h"] + C.RESET +
            t_str +
            bc + BOX["h"] * fill + BOX["tr"] + C.RESET
        )
    else:
        out.append(" " * indent + bc + BOX["tl"] + BOX["h"] * (w-2) + BOX["tr"] + C.RESET)

    # Content lines
    for line in lines:
        # Wrap long lines
        import re
        vis = re.sub(r'\033\[[0-9;]*m', '', line)
        if len(vis) > inner:
            # Simple truncate with indicator
            line = line[:inner-1] + dim("…")
        out.append(
            " " * indent +
            bc + BOX["v"] + C.RESET +
            " " +
            _pad(line, inner) +
            " " +
            bc + BOX["v"] + C.RESET
        )

    # Bottom border
    out.append(" " * indent + bc + BOX["bl"] + BOX["h"] * (w-2) + BOX["br"] + C.RESET)

    return "\n".join(out)


# ── Spezialisierte Print-Funktionen ──────────────────────────────────────────

def print_header(model: str, fallback: str, tools: int, workspace: str, iteration: int = 0) -> None:
    w = min(term_width(), 100)
    print()
    print(f"  {C.BOLD}{C.BCYAN}◆ ai-coder{C.RESET}  "
          f"{dim('model='+ model)}  "
          f"{dim('tools='+str(tools))}  "
          f"{dim('ws='+workspace)}")
    print(f"  {C.DIM}{'─' * (w-4)}{C.RESET}")


def print_task(prompt: str) -> None:
    """Display user task with style."""
    print()
    print(f"  {C.BOLD}{C.BWHITE}▸ Task{C.RESET}")
    for line in prompt.splitlines():
        print(f"  {C.WHITE}{line}{C.RESET}")
    print()


def print_thinking(iteration: int, model: str) -> None:
    """'Thinking...' marker."""
    iter_str = f"  step {iteration}" if iteration > 0 else ""
    print(f"  {C.DIM}{C.CYAN}◉{C.RESET}  {dim('thinking'+ iter_str + '…')}", end="\r", flush=True)


def print_thought(text: str) -> None:
    """LLM thoughts (before tool call) — dimmed."""
    if not text.strip():
        return
    print(f"  {C.DIM}│{C.RESET}")
    for line in text.strip().splitlines()[:8]:
        print(f"  {C.DIM}│  {line[:120]}{C.RESET}")
    print(f"  {C.DIM}│{C.RESET}")


def print_tool_call(name: str, args: dict, iteration: int) -> None:
    """Tool call header — opencode style."""
    _, col = _spinner_for(name)
    # Args als kompakte key=val string
    def fmt_arg(k: str, v: Any) -> str:
        if isinstance(v, str) and len(v) > 60:
            v = v[:57] + "…"
        if isinstance(v, list):
            v = "[" + ", ".join(str(x) for x in v[:3]) + (", …" if len(v) > 3 else "") + "]"
        return f"{dim(k+'=')}{v}"
    args_str = "  ".join(fmt_arg(k, v) for k, v in list(args.items())[:4])

    print(f"\n  {col}{C.BOLD}⟡ {name}{C.RESET}  {args_str}")


def print_tool_result(name: str, result: str, elapsed: float, error: bool = False) -> None:
    """Tool result in a panel."""
    _, col = _spinner_for(name)

    if error:
        title_color = C.BRED
        border_color = C.BRED + C.DIM
        prefix = "✗ "
    else:
        title_color = col
        border_color = C.DIM
        prefix = ""

    # JSON pretty-print if possible
    try:
        data = json.loads(result)
        if isinstance(data, dict) and len(result) < 3000:
            lines = []
            for k, v in data.items():
                vstr = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
                vstr = vstr[:200] + "…" if len(vstr) > 200 else vstr
                lines.append(f"{dim(k+':')} {vstr}")
            display = "\n".join(lines[:30])
        else:
            display = result[:2000]
    except Exception:
        display = result[:2000]

    title = f"{prefix}{name}  {C.DIM}{elapsed:.2f}s{C.RESET}"
    print(panel(display, title=title, title_color=title_color, border_color=border_color, max_lines=35))


def print_final(response: str, model: str, latency_ms: Any, total_iters: int,
                fallback_used: bool = False) -> None:
    """Final agent response."""
    # DONE: prefix entfernen
    import re
    text = re.sub(r'^DONE:\s*', '', response.strip(), flags=re.IGNORECASE)

    print()
    print(f"  {C.BGREEN}{C.BOLD}✓ Done{C.RESET}  {dim(str(total_iters)+' step'+('s' if total_iters!=1 else ''))}  "
          f"{dim(str(latency_ms)+'ms') if latency_ms else ''}"
          f"{'  ' + C.BYELLOW + 'FALLBACK' + C.RESET if fallback_used else ''}")
    print()

    # Response rendern
    for line in text.splitlines():
        # Minimal code block highlighting
        if line.startswith("```"):
            print(f"  {C.DIM}{'─'*50}{C.RESET}")
        elif line.startswith("#"):
            print(f"  {C.BOLD}{C.BWHITE}{line}{C.RESET}")
        elif line.startswith("- ") or line.startswith("* "):
            print(f"  {C.CYAN}•{C.RESET} {line[2:]}")
        elif line.startswith("  ") and (line.strip().startswith("$") or line.strip().startswith(">")):
            print(f"  {C.BGREEN}{line}{C.RESET}")
        else:
            print(f"  {line}")
    print()
    print(f"  {C.DIM}{'─'*50}{C.RESET}")
    print(f"  {dim(model)}  {dim('steps='+str(total_iters))}")
    print()


def print_error(msg: str) -> None:
    print(f"\n  {C.BRED}✗{C.RESET} {msg}")


def print_interrupted() -> None:
    print(f"\n  {C.BYELLOW}◌{C.RESET} {dim('interrupted')}")


def print_max_iter(n: int) -> None:
    print(f"\n  {C.BYELLOW}◎{C.RESET} {dim(f'max iterations ({n}) reached')}")
