"""Settings-Tab fuer ai-coder GUI."""
from __future__ import annotations
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLineEdit, QPushButton, QComboBox, QLabel, QGroupBox, QMessageBox,
)
from PyQt6.QtCore import Qt

from ..config import DEFAULT_BASE_URL, Session, load_session, save_session, delete_session
from ..session_state import SWARM_MODES, get_state, set_model, set_fallback, set_swarm
from ..client import TriForceClient, ClientError


class SettingsWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
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
        self.model_edit = QLineEdit()
        self.model_edit.setPlaceholderText("z.B. anthropic/claude-sonnet-4")
        self.fallback_edit = QLineEdit()
        self.fallback_edit.setPlaceholderText("z.B. gemini/gemini-2.0-flash")
        self.swarm_combo = QComboBox()
        self.swarm_combo.addItems(sorted(SWARM_MODES))
        model_form.addRow("Modell:", self.model_edit)
        model_form.addRow("Fallback:", self.fallback_edit)
        model_form.addRow("Swarm:", self.swarm_combo)

        save_btn = QPushButton("Speichern")
        save_btn.clicked.connect(self._save_model_config)
        model_form.addRow(save_btn)
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
        except Exception:
            self.status_label.setText("Nicht eingeloggt")
            self.status_label.setStyleSheet("color: #ff6b6b;")

        # State
        state = get_state()
        if state.get("selected_model"):
            self.model_edit.setText(state["selected_model"])
        if state.get("fallback_model"):
            self.fallback_edit.setText(state["fallback_model"])
        idx = self.swarm_combo.findText(state.get("swarm_mode", "off"))
        if idx >= 0:
            self.swarm_combo.setCurrentIndex(idx)

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
        except (ClientError, Exception) as e:
            QMessageBox.critical(self, "Login fehlgeschlagen", str(e))

    def _do_logout(self):
        delete_session()
        self.status_label.setText("Nicht eingeloggt")
        self.status_label.setStyleSheet("color: #ff6b6b;")

    def _save_model_config(self):
        model = self.model_edit.text().strip()
        fallback = self.fallback_edit.text().strip()
        swarm = self.swarm_combo.currentText()
        if model:
            set_model(model)
        if fallback:
            set_fallback(fallback)
        set_swarm(swarm)
        self.status_label.setText("Gespeichert.")
        self.status_label.setStyleSheet("color: #00ff88;")

    def get_current_model(self) -> str:
        return self.model_edit.text().strip()

    def get_current_fallback(self) -> str:
        return self.fallback_edit.text().strip()
