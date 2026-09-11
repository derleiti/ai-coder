"""Local Ollama + Hugging Face GGUF model manager."""
from __future__ import annotations

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel, QPushButton,
    QLineEdit, QComboBox, QListWidget, QListWidgetItem, QMessageBox,
)

from ..local_models import (
    LocalModelError, download_gguf, host_status, huggingface_gguf_files,
    import_gguf, installed_models, launch_ollama_installer, search_huggingface,
    suggested_host_name, test_model,
)


def _size(value: int) -> str:
    n = float(value or 0)
    if n <= 0:
        return "?"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return "?"


class _Worker(QThread):
    done = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn, self.args, self.kwargs = fn, args, kwargs

    def run(self):
        try:
            self.done.emit(self.fn(*self.args, **self.kwargs))
        except Exception as exc:
            self.error.emit(str(exc))


class LocalModelsWidget(QWidget):
    """Explicit local hosting workflow. It never publishes Ollama externally."""
    model_selected = pyqtSignal(str)
    models_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker = None
        # Keep finished QThread wrappers alive until Qt has delivered every
        # queued signal. Replacing the sole reference from inside a completion
        # callback can otherwise destroy the emitting QThread and abort Qt.
        self._workers = set()
        self._repos = []
        self._files = []
        self._build_ui()
        self.refresh_host()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        runtime = QGroupBox("Lokaler Modell-Host · Ollama")
        row = QHBoxLayout(runtime)
        self.host_label = QLabel("Prüfe lokalen Dienst …")
        self.install_btn = QPushButton("Ollama einrichten")
        self.install_btn.clicked.connect(self._install)
        refresh = QPushButton("Aktualisieren")
        refresh.clicked.connect(self.refresh_host)
        row.addWidget(self.host_label, 1)
        row.addWidget(self.install_btn)
        row.addWidget(refresh)
        layout.addWidget(runtime)

        installed = QGroupBox("Installierte / gehostete Modelle")
        installed_layout = QVBoxLayout(installed)
        self.installed = QComboBox()
        self.installed.setMinimumWidth(420)
        buttons = QHBoxLayout()
        test = QPushButton("Modell testen")
        test.clicked.connect(self._test_selected)
        use = QPushButton("Als Basismodell verwenden")
        use.clicked.connect(self._use_selected)
        buttons.addWidget(test)
        buttons.addWidget(use)
        buttons.addStretch()
        installed_layout.addWidget(self.installed)
        installed_layout.addLayout(buttons)
        layout.addWidget(installed)

        hf = QGroupBox("Hugging Face · GGUF suchen, herunterladen und hosten")
        hf_layout = QVBoxLayout(hf)
        search_row = QHBoxLayout()
        self.query = QLineEdit()
        self.query.setPlaceholderText("z. B. Qwen Coder, Llama, Mistral …")
        self.query.returnPressed.connect(self.search)
        search = QPushButton("HF durchsuchen")
        search.clicked.connect(self.search)
        search_row.addWidget(self.query, 1)
        search_row.addWidget(search)
        hf_layout.addLayout(search_row)

        self.repos = QListWidget()
        self.repos.setMinimumHeight(130)
        self.repos.currentRowChanged.connect(self._repo_changed)
        hf_layout.addWidget(self.repos)

        file_row = QHBoxLayout()
        file_row.addWidget(QLabel("GGUF-Datei / Quantisierung:"))
        self.files = QComboBox()
        self.files.currentIndexChanged.connect(self._file_changed)
        file_row.addWidget(self.files, 1)
        hf_layout.addLayout(file_row)

        host_row = QHBoxLayout()
        host_row.addWidget(QLabel("Lokaler Modellname:"))
        self.host_name = QLineEdit()
        host_row.addWidget(self.host_name, 1)
        self.host_btn = QPushButton("Download & Hosten")
        self.host_btn.clicked.connect(self.download_and_host)
        host_row.addWidget(self.host_btn)
        hf_layout.addLayout(host_row)

        self.status = QLabel("Nur GGUF wird angeboten. Download bleibt lokal; keine öffentliche Freigabe.")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: #888; font-size: 11px;")
        hf_layout.addWidget(self.status)
        layout.addWidget(hf)

    def _start(self, fn, callback, *args, **kwargs):
        # One operation at a time keeps repository/file selections coherent.
        if any(worker.isRunning() for worker in self._workers):
            return False
        self.setEnabled(False)
        worker = _Worker(fn, *args, **kwargs)
        self._worker = worker
        self._workers.add(worker)
        worker.done.connect(callback)
        worker.error.connect(self._error)
        worker.finished.connect(lambda w=worker: self._worker_finished(w))
        worker.start()
        return True

    def _worker_finished(self, worker):
        self.setEnabled(True)
        # deleteLater is safe now that QThread has emitted finished; retain the
        # Python wrapper until this slot runs so it cannot be collected early.
        self._workers.discard(worker)
        if self._worker is worker:
            self._worker = None
        worker.deleteLater()

    def _error(self, text: str):
        self.status.setText(text)
        self.status.setStyleSheet("color: #ff6b6b; font-size: 11px;")

    def refresh_host(self):
        self._start(lambda: (host_status(), installed_models() if host_status().api_online else []), self._host_loaded)

    def _host_loaded(self, payload):
        status, models = payload
        if status.api_online:
            self.host_label.setText(f"● Online · {status.ollama_version or 'Ollama'} · {status.base_url} · {len(models)} Modelle")
            self.host_label.setStyleSheet("color: #00ff88;")
        elif status.ollama_installed:
            self.host_label.setText(f"● Installiert, Dienst {status.service_state} · {status.base_url}")
            self.host_label.setStyleSheet("color: #ffb020;")
        else:
            self.host_label.setText("● Ollama fehlt · Einrichtung erforderlich")
            self.host_label.setStyleSheet("color: #ff6b6b;")
        self.install_btn.setText("Ollama reparieren/einrichten" if status.ollama_installed else "Ollama installieren")
        current = self.installed.currentData()
        self.installed.clear()
        for model in models:
            label = f"{model['name']} · {_size(model['size'])}"
            if model.get("quantization"):
                label += f" · {model['quantization']}"
            self.installed.addItem(label, model["id"])
        if current:
            idx = self.installed.findData(current)
            if idx >= 0:
                self.installed.setCurrentIndex(idx)

    def _install(self):
        try:
            launch_ollama_installer()
            self.status.setText("Ollama-Setup wurde in einem sichtbaren Terminal geöffnet. Danach hier Aktualisieren drücken.")
            self.status.setStyleSheet("color: #00d4ff; font-size: 11px;")
        except LocalModelError as exc:
            self._error(str(exc))

    def search(self):
        query = self.query.text().strip()
        self.status.setText("Durchsuche Hugging Face nach GGUF-Modellen …")
        self._start(search_huggingface, self._search_loaded, query, limit=50)

    def _search_loaded(self, rows):
        self._repos = list(rows)
        self.repos.clear()
        for row in self._repos:
            gated = " · gated" if row.gated else ""
            item = QListWidgetItem(f"{row.repo_id} · ↓ {row.downloads:,} · ♥ {row.likes}{gated}")
            item.setData(256, row.repo_id)
            self.repos.addItem(item)
        self.status.setText(f"{len(rows)} GGUF-Repositories gefunden. Modell wählen, dann Quantisierung auswählen.")
        self.status.setStyleSheet("color: #00d4ff; font-size: 11px;")
        if rows:
            self.repos.setCurrentRow(0)

    def _repo_changed(self, row: int):
        if row < 0 or row >= len(self._repos):
            return
        repo = self._repos[row].repo_id
        self.files.clear()
        self.status.setText(f"Lade GGUF-Dateien von {repo} …")
        self._start(huggingface_gguf_files, self._files_loaded, repo)

    def _files_loaded(self, rows):
        self._files = list(rows)
        self.files.clear()
        for row in self._files:
            quant = f" · {row.quantization}" if row.quantization else ""
            self.files.addItem(f"{row.filename} · {_size(row.size)}{quant}", row.filename)
        self.status.setText(f"{len(rows)} GGUF-Dateien verfügbar.")
        self.status.setStyleSheet("color: #00d4ff; font-size: 11px;")
        self._file_changed(0)

    def _file_changed(self, index: int):
        row = self.repos.currentRow()
        if row < 0 or row >= len(self._repos) or index < 0 or index >= len(self._files):
            return
        self.host_name.setText(suggested_host_name(self._repos[row].repo_id, self._files[index].filename))

    def download_and_host(self):
        repo_index = self.repos.currentRow()
        file_index = self.files.currentIndex()
        if repo_index < 0 or repo_index >= len(self._repos) or file_index < 0 or file_index >= len(self._files):
            QMessageBox.warning(self, "Local Models", "Bitte zuerst Repository und GGUF-Datei auswählen.")
            return
        repo = self._repos[repo_index].repo_id
        filename = self._files[file_index].filename
        name = self.host_name.text().strip()
        if not name:
            QMessageBox.warning(self, "Local Models", "Bitte einen lokalen Modellnamen angeben.")
            return
        answer = QMessageBox.question(
            self, "GGUF herunterladen und hosten",
            f"{repo}\n{filename}\n\nDownload starten und anschließend als '{name}' in Ollama importieren?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.status.setText("Download läuft … große GGUF-Dateien können mehrere Minuten dauern.")
        self._start(self._download_import, self._hosted, repo, filename, name)

    @staticmethod
    def _download_import(repo: str, filename: str, name: str):
        path = download_gguf(repo, filename)
        model_id = import_gguf(path, name)
        return {"model": model_id, "path": str(path)}

    def _hosted(self, result):
        self.status.setText(f"Gehostet: {result['model']} · lokaler Download: {result['path']}")
        self.status.setStyleSheet("color: #00ff88; font-size: 11px;")
        self.models_changed.emit()
        self.refresh_host()

    def _test_selected(self):
        model = self.installed.currentData()
        if not model:
            return
        self.status.setText(f"Teste {model} …")
        self._start(test_model, self._tested, model)

    def _tested(self, result):
        self.status.setText(f"Test OK · {result['model']} → {result['response']}")
        self.status.setStyleSheet("color: #00ff88; font-size: 11px;")

    def _use_selected(self):
        model = str(self.installed.currentData() or "")
        if model:
            self.model_selected.emit(model)
