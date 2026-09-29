"""Optional local voice input for AICoder.

The voice layer is deliberately dumb: local PCM -> Vosk text -> wake-word routing.
It never calls an LLM and never sends audio over the network.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import threading
import time
from typing import Callable

DEFAULT_WAKE_WORD = "nova"
DEFAULT_MODEL_NAME = "vosk-model-small-de-0.15"
DEFAULT_SAMPLE_RATE = 16_000


def default_model_path() -> Path:
    override = os.environ.get("AICODER_VOICE_MODEL", "").strip()
    if override:
        return Path(override).expanduser()
    base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return base / "ailinux" / "aicoder" / "voice" / DEFAULT_MODEL_NAME


def capture_command() -> list[str]:
    override = os.environ.get("AICODER_VOICE_CAPTURE_CMD", "").strip()
    if override:
        return shlex.split(override)
    if shutil.which("parec"):
        return [
            "parec", "--raw", "--device=@DEFAULT_SOURCE@",
            f"--rate={DEFAULT_SAMPLE_RATE}", "--format=s16le", "--channels=1",
            "--client-name=AICoder Nova Voice",
        ]
    if shutil.which("pw-record"):
        return [
            "pw-record", "--raw", "--rate", str(DEFAULT_SAMPLE_RATE),
            "--channels", "1", "--format", "s16", "-",
        ]
    if shutil.which("arecord"):
        return [
            "arecord", "-q", "-t", "raw", "-f", "S16_LE",
            "-r", str(DEFAULT_SAMPLE_RATE), "-c", "1",
        ]
    return []


def sanitize_tts_text(text: str, limit: int = 1600) -> str:
    """Turn a model response into bounded, speech-friendly plain text."""
    value = str(text or "")
    lines: list[str] = []
    in_code = False
    for raw in value.splitlines():
        line = raw.strip()
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        line = line.replace("`", "").replace("**", "").replace("__", "")
        line = line.lstrip("#>*- ").strip()
        if line:
            lines.append(line)
    spoken = " ".join(lines)
    if len(spoken) > limit:
        spoken = spoken[:limit].rsplit(" ", 1)[0] + " …"
    return spoken


def tts_available() -> bool:
    return bool(shutil.which("spd-say"))


def speak_text(text: str, *, language: str = "de") -> None:
    spoken = sanitize_tts_text(text)
    if not spoken:
        return
    binary = shutil.which("spd-say")
    if not binary:
        raise RuntimeError("Speech Dispatcher (spd-say) ist nicht installiert.")
    subprocess.run(
        [binary, "-w", "-l", language, "-m", "none", "-N", "AICoder", "-n", "nova", spoken],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def cancel_speech() -> None:
    binary = shutil.which("spd-say")
    if binary:
        subprocess.run([binary, "-C"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def runtime_status(model_path: Path | None = None) -> dict[str, object]:
    model = Path(model_path or default_model_path())
    try:
        import vosk  # noqa: F401
        vosk_ok = True
    except Exception:
        vosk_ok = False
    cmd = capture_command()
    return {
        "ready": bool(vosk_ok and model.is_dir() and cmd),
        "vosk": vosk_ok,
        "model": str(model),
        "model_exists": model.is_dir(),
        "capture_command": cmd,
    }


def _normalized_words(text: str) -> list[str]:
    cleaned = "".join(ch.lower() if ch.isalnum() or ch in "äöüß " else " " for ch in str(text or ""))
    return [part for part in cleaned.split() if part]


@dataclass
class WakeWordRouter:
    wake_word: str = DEFAULT_WAKE_WORD
    command_window_s: float = 8.0
    armed_until: float = 0.0

    def accept(self, text: str, *, now: float | None = None) -> str | None:
        """Return a command, ``""`` for wake-only, or ``None`` for no match."""
        stamp = time.monotonic() if now is None else float(now)
        words = _normalized_words(text)
        if not words:
            return None
        wake = self.wake_word.strip().lower()
        if wake in words:
            index = words.index(wake)
            remainder = " ".join(words[index + 1:]).strip()
            self.armed_until = stamp + self.command_window_s
            if remainder:
                self.armed_until = 0.0
                return remainder
            return ""
        if self.armed_until and stamp <= self.armed_until:
            self.armed_until = 0.0
            return " ".join(words)
        if self.armed_until and stamp > self.armed_until:
            self.armed_until = 0.0
        return None


class VoskVoiceListener:
    def __init__(
        self,
        *,
        model_path: Path | None = None,
        wake_word: str = DEFAULT_WAKE_WORD,
        command_window_s: float = 8.0,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
    ) -> None:
        self.model_path = Path(model_path or default_model_path())
        self.sample_rate = int(sample_rate)
        self.router = WakeWordRouter(wake_word=wake_word, command_window_s=command_window_s)
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()

    def stop(self) -> None:
        with self._lock:
            proc = self._process
        if proc is not None and proc.poll() is None:
            proc.terminate()

    def run(
        self,
        stop_event: threading.Event,
        *,
        on_state: Callable[[str], None] | None = None,
        on_partial: Callable[[str], None] | None = None,
        on_command: Callable[[str], None] | None = None,
    ) -> None:
        try:
            from vosk import KaldiRecognizer, Model, SetLogLevel
        except Exception as exc:
            raise RuntimeError("Vosk ist nicht installiert. Installiere AICoder mit dem Voice-Extra.") from exc
        if not self.model_path.is_dir():
            raise RuntimeError(f"Vosk-Modell fehlt: {self.model_path}")
        cmd = capture_command()
        if not cmd:
            raise RuntimeError("Kein lokaler Audio-Capture-Backend gefunden (parec/pw-record/arecord).")

        SetLogLevel(-1)
        if on_state:
            on_state("loading")
        model = Model(str(self.model_path))
        recognizer = KaldiRecognizer(model, self.sample_rate)

        if on_state:
            on_state("listening")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        with self._lock:
            self._process = proc
        try:
            assert proc.stdout is not None
            while not stop_event.is_set():
                chunk = proc.stdout.read(4000)
                if not chunk:
                    if proc.poll() is not None:
                        break
                    continue
                if recognizer.AcceptWaveform(chunk):
                    text = str(json.loads(recognizer.Result()).get("text") or "").strip()
                    routed = self.router.accept(text)
                    if routed == "":
                        if on_state:
                            on_state("armed")
                    elif routed:
                        if on_command:
                            on_command(routed)
                        if on_state:
                            on_state("listening")
                elif on_partial:
                    partial = str(json.loads(recognizer.PartialResult()).get("partial") or "").strip()
                    if partial:
                        on_partial(partial)
        finally:
            with self._lock:
                self._process = None
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.kill()
            if on_state:
                on_state("stopped")
