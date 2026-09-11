from __future__ import annotations
import os, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication
from aicoder.gui.local_models_widget import LocalModelsWidget
from aicoder.local_models import HFFile, HFModel


def _drain(app, widget, timeout=3):
    deadline=time.time()+timeout
    while widget._workers and time.time()<deadline:
        app.processEvents(); time.sleep(.005)
    app.processEvents()


def test_hf_search_worker_survives_chained_file_lookup(monkeypatch):
    app=QApplication.instance() or QApplication([])
    monkeypatch.setattr("aicoder.gui.local_models_widget.host_status", lambda: type("S",(),{
        "api_online":False,"ollama_installed":False,"service_state":"inactive",
        "base_url":"http://127.0.0.1:11434","ollama_version":""})())
    monkeypatch.setattr("aicoder.gui.local_models_widget.search_huggingface", lambda q, limit=40: [HFModel("org/demo-GGUF", 10, 2)])
    monkeypatch.setattr("aicoder.gui.local_models_widget.huggingface_gguf_files", lambda repo: [HFFile(repo,"demo-Q4_K_M.gguf",1234,"Q4_K_M")])
    widget=LocalModelsWidget(); _drain(app,widget)
    widget.query.setText("demo")
    widget.search(); _drain(app,widget); _drain(app,widget)
    assert widget.repos.count()==1
    assert widget.files.count()==1
    assert not widget._workers
    assert widget.isEnabled()
    widget.close(); app.processEvents()
