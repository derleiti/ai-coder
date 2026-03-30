"""Settings-Tab — Login, Model-Dropdown, Fallback-Dropdown, Swarm."""
from __future__ import annotations
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLineEdit, QPushButton, QComboBox, QLabel, QGroupBox, QMessageBox,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal

from ..config import DEFAULT_BASE_URL, Session, load_session, save_session, delete_session
from ..session_state import SWARM_MODES, get_state, set_model, set_fallback, set_swarm
from ..client import TriForceClient, ClientError


class _ModelLoader(QThread):
    """Laedt Modell-Liste vom Backend im Hintergrund."""
    loaded = pyqtSignal(list, str)   # (models, tier)
    error = pyqtSignal(str)

    def __init__(self, client):
        super().__init__()
        self.client = client

    def run(self):
        try:
            data = self.client._request(
                "GET", "/v1/client/models",
                require_auth=True, _label="models"
            )
            models = data.get("models", [])
            tier = data.get("tier", "?")
            self.loaded.emit(models, tier)
        except Exception as e:
            self.error.emit(str(e))


class SettingsWidget(QWidget):
    models_loaded = pyqtSignal(list)  # emitted with sorted model list

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loader = None
        self._models = []
        self._build_ui()
        self._load_current()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)

        # --- Login Group ---
        login_group = QGroupBox("Login")
        login_form = QFormLayout()
        self.base_url_edit = QLineEdit(DEFAULT_BASE_URL)
        self.email_edit = QLineEdit()
        self.email_edit.setPlaceholderText("user@example.com")
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setPlaceholderText("Passwort")
        login_form.addRow("Base URL:", self.base_url_edit)
        login_form.addRow("E-Mail:", self.email_edit)
        login_form.addRow("Passwort:", self.password_edit)

        btn_row = QHBoxLayout()
        self.login_btn = QPushButton("Login")
        self.login_btn.clicked.connect(self._do_login)
        self.logout_btn = QPushButton("Logout")
        self.logout_btn.clicked.connect(self._do_logout)
        self.status_label = QLabel("")
        btn_row.addWidget(self.login_btn)
        btn_row.addWidget(self.logout_btn)
        btn_row.addWidget(self.status_label)
        btn_row.addStretch()
        login_form.addRow(btn_row)
        login_group.setLayout(login_form)
        layout.addWidget(login_group)

        # --- Model Group ---
        model_group = QGroupBox("Modell-Konfiguration")
        model_form = QFormLayout()

        # Model Dropdown (editable — user can type custom model too)
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.model_combo.lineEdit().setPlaceholderText("Modell waehlen oder eingeben...")

        # Fallback Dropdown
        self.fallback_combo = QComboBox()
        self.fallback_combo.setEditable(True)
        self.fallback_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.fallback_combo.lineEdit().setPlaceholderText("Fallback waehlen...")

        # Refresh button
        refresh_btn = QPushButton("Modelle laden")
        refresh_btn.clicked.connect(self._load_models)

        self.model_status = QLabel("")
        self.model_status.setStyleSheet("color: #888; font-size: 11px;")

        model_form.addRow("Modell:", self.model_combo)
        model_form.addRow("Fallback:", self.fallback_combo)

        # Swarm
        self.swarm_combo = QComboBox()
        self.swarm_combo.addItems(sorted(SWARM_MODES))
        model_form.addRow("Swarm:", self.swarm_combo)

        # Buttons row
        model_btn_row = QHBoxLayout()
        save_btn = QPushButton("Speichern")
        save_btn.clicked.connect(self._save_model_config)
        model_btn_row.addWidget(refresh_btn)
        model_btn_row.addWidget(save_btn)
        model_btn_row.addWidget(self.model_status)
        model_btn_row.addStretch()
        model_form.addRow(model_btn_row)

        model_group.setLayout(model_form)
        layout.addWidget(model_group)

        layout.addStretch()

    def _load_current(self):
        # Session
        try:
            session = load_session()
            self.base_url_edit.setText(session.base_url)
            self.status_label.setText(f"Eingeloggt als {session.user_id} ({session.tier})")
            self.status_label.setStyleSheet("color: #00d4ff;")
            # Auto-load models on startup if logged in
            self._load_models()
        except Exception:
            self.status_label.setText("Nicht eingeloggt")
            self.status_label.setStyleSheet("color: #ff6b6b;")

        # State
        state = get_state()
        if state.get("selected_model"):
            self.model_combo.setCurrentText(state["selected_model"])
        if state.get("fallback_model"):
            self.fallback_combo.setCurrentText(state["fallback_model"])
        idx = self.swarm_combo.findText(state.get("swarm_mode", "off"))
        if idx >= 0:
            self.swarm_combo.setCurrentIndex(idx)

    def _load_models(self):
        """Lade Modell-Liste vom Backend."""
        try:
            session = load_session()
            client = TriForceClient(session.base_url, token=session.token, timeout=10)
        except Exception:
            self.model_status.setText("Nicht eingeloggt")
            self.model_status.setStyleSheet("color: #ff6b6b; font-size: 11px;")
            return

        self.model_status.setText("Lade Modelle...")
        self.model_status.setStyleSheet("color: #00d4ff; font-size: 11px;")

        self._loader = _ModelLoader(client)
        self._loader.loaded.connect(self._on_models_loaded)
        self._loader.error.connect(self._on_models_error)
        self._loader.start()

    def _on_models_loaded(self, models: list, tier: str):
        self._models = sorted(models)

        # Save current selection
        cur_model = self.model_combo.currentText()
        cur_fallback = self.fallback_combo.currentText()

        # Populate dropdowns
        self.model_combo.clear()
        self.fallback_combo.clear()
        self.fallback_combo.addItem("")  # empty = no fallback

        for m in self._models:
            self.model_combo.addItem(m)
            self.fallback_combo.addItem(m)

        # Restore selection
        if cur_model:
            self.model_combo.setCurrentText(cur_model)
        if cur_fallback:
            self.fallback_combo.setCurrentText(cur_fallback)

        self.model_status.setText(f"{len(models)} Modelle ({tier})")
        self.model_status.setStyleSheet("color: #00ff88; font-size: 11px;")
        self.models_loaded.emit(self._models)

    def _on_models_error(self, err: str):
        self.model_status.setText(f"Fehler: {err[:60]}")
        self.model_status.setStyleSheet("color: #ff6b6b; font-size: 11px;")

    def _do_login(self):
        base_url = self.base_url_edit.text().strip()
        email = self.email_edit.text().strip()
        password = self.password_edit.text()
        if not email or not password:
            QMessageBox.warning(self, "Login", "E-Mail und Passwort eingeben.")
            return
        try:
            client = TriForceClient(base_url)
            result = client.login(email, password)
            session = Session(
                base_url=base_url,
                token=result["token"],
                client_id=result.get("client_id", ""),
                user_id=result.get("user_id", email),
                tier=result.get("tier", "unknown"),
                account_role=result.get("account_role", "unknown"),
            )
            save_session(session)
            self.password_edit.clear()
            self.status_label.setText(f"Eingeloggt als {session.user_id} ({session.tier})")
            self.status_label.setStyleSheet("color: #00d4ff;")
            # Auto-load models after login
            self._load_models()
        except (ClientError, Exception) as e:
            QMessageBox.critical(self, "Login fehlgeschlagen", str(e))

    def _do_logout(self):
        delete_session()
        self.status_label.setText("Nicht eingeloggt")
        self.status_label.setStyleSheet("color: #ff6b6b;")
        self.model_combo.clear()
        self.fallback_combo.clear()
        self._models = []

    def _save_model_config(self):
        model = self.model_combo.currentText().strip()
        fallback = self.fallback_combo.currentText().strip()
        swarm = self.swarm_combo.currentText()
        if model:
            set_model(model)
        if fallback:
            set_fallback(fallback)
        set_swarm(swarm)
        self.model_status.setText("Gespeichert.")
        self.model_status.setStyleSheet("color: #00ff88; font-size: 11px;")

    def get_current_model(self) -> str:
        return self.model_combo.currentText().strip()

    def get_current_fallback(self) -> str:
        return self.fallback_combo.currentText().strip()
