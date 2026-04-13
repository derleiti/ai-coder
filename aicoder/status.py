from __future__ import annotations
import itertools, sys, threading, time

class Spinner:
    def __init__(self, text: str, file=None):
        self.text = text
        self.file = file or sys.stderr   # stderr: JSON auf stdout bleibt sauber
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        self.file.write("\r" + " " * (len(self.text) + 10) + "\r")
        self.file.flush()

    def _run(self):
        for ch in itertools.cycle("|/-\\"):
            if self._stop.is_set():
                break
            self.file.write(f"\r{self.text} {ch}")
            self.file.flush()
            time.sleep(0.1)

def phase_label(mode: str) -> str:
    mode = (mode or "").strip().lower()
    if mode in {"swarm", "swarming"}:
        return "swarming..."
    if mode in {"hive", "hivemind", "hiveing"}:
        return "hiveing..."
    return "working..."
