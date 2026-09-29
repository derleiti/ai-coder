from __future__ import annotations

from pathlib import Path

from aicoder import voice


def test_wake_word_with_inline_command():
    router = voice.WakeWordRouter()
    assert router.accept("Nova prüfe den Build", now=10) == "prüfe den build"


def test_wake_word_arms_next_phrase():
    router = voice.WakeWordRouter(command_window_s=8)
    assert router.accept("Nova", now=10) == ""
    assert router.accept("öffne das Projekt", now=12) == "öffne das projekt"


def test_armed_window_expires():
    router = voice.WakeWordRouter(command_window_s=3)
    assert router.accept("Nova", now=10) == ""
    assert router.accept("das ist zu spät", now=14) is None


def test_unrelated_speech_is_ignored():
    router = voice.WakeWordRouter()
    assert router.accept("wir testen den Build", now=1) is None


def test_capture_override(monkeypatch):
    monkeypatch.setenv("AICODER_VOICE_CAPTURE_CMD", "capture --rate 16000 --mono")
    assert voice.capture_command() == ["capture", "--rate", "16000", "--mono"]


def test_model_override(monkeypatch, tmp_path: Path):
    target = tmp_path / "model"
    monkeypatch.setenv("AICODER_VOICE_MODEL", str(target))
    assert voice.default_model_path() == target


def test_sanitize_tts_text_removes_code_blocks_and_markdown():
    source = "**Fertig.**\n```bash\nsudo reboot\n```\n- Der Build ist grün."
    assert voice.sanitize_tts_text(source) == "Fertig. Der Build ist grün."


def test_sanitize_tts_text_is_bounded():
    spoken = voice.sanitize_tts_text("wort " * 1000, limit=80)
    assert len(spoken) <= 82
    assert spoken.endswith("…")
