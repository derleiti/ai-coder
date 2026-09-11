from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aicoder.local_models import (
    huggingface_gguf_files, installed_models, sanitize_host_name,
    search_huggingface, suggested_host_name,
)


def test_sanitize_host_name_is_stable():
    assert sanitize_host_name(" Qwen 2.5 / Coder : Q4_K_M ") == "qwen-2.5-coder-:-q4_k_m"


def test_suggested_host_name_uses_repo_and_quant():
    name = suggested_host_name("bartowski/Qwen2.5-Coder-7B-Instruct-GGUF", "Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf")
    assert name == "hf-qwen2.5-coder-7b-instruct:q4_k_m"


def test_installed_models_are_namespaced(monkeypatch):
    monkeypatch.setattr("aicoder.local_models._api_json", lambda *a, **k: {"models": [{
        "name": "qwen:7b", "size": 123, "details": {"quantization_level": "Q4_K_M", "family": "qwen"}
    }]})
    assert installed_models() == [{
        "name": "qwen:7b", "id": "ollama/qwen:7b", "size": 123,
        "modified_at": "", "quantization": "Q4_K_M", "family": "qwen",
    }]


def test_hf_search_requests_gguf_filter():
    captured = {}
    def list_models(**kwargs):
        captured.update(kwargs)
        return [SimpleNamespace(
        id="org/model-GGUF", downloads=12, likes=3, pipeline_tag="text-generation",
        gated=False, last_modified="today",
        )]
    api = SimpleNamespace(list_models=list_models)
    with patch("aicoder.local_models._hf_api", return_value=api):
        rows = search_huggingface("qwen", limit=5)
    assert rows[0].repo_id == "org/model-GGUF"
    assert captured["filter"] == "gguf"
    assert captured["sort"] == "downloads"
    assert "direction" not in captured


def test_hf_files_only_returns_gguf():
    api = SimpleNamespace(model_info=lambda *a, **k: SimpleNamespace(siblings=[
        SimpleNamespace(rfilename="README.md", size=10),
        SimpleNamespace(rfilename="model-Q4_K_M-00001-of-00002.gguf", size=2000),
        SimpleNamespace(rfilename="model-Q4_K_M.gguf", size=4000),
    ]))
    with patch("aicoder.local_models._hf_api", return_value=api):
        rows = huggingface_gguf_files("org/model")
    assert [(r.filename, r.quantization) for r in rows] == [("model-Q4_K_M.gguf", "Q4_K_M")]
