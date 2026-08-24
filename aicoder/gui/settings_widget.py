"""Settings tab — login, base model, team models, tools and runtime."""
from __future__ import annotations
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLineEdit, QPushButton, QComboBox, QLabel, QGroupBox, QMessageBox,
    QListWidget, QListWidgetItem, QSpinBox, QSizePolicy, QScrollArea, QCheckBox,
)
from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal

from ..config import DEFAULT_BASE_URL, Session, load_session, save_session, delete_session
from ..session_state import (
    SWARM_MODES, get_state, set_model, set_swarm,
    set_tool_mode, set_enabled_tools, set_request_timeout,
    set_approval_mode, set_native_openrouter_tool_calling,
)
from ..client import TriForceClient, model_identifier
from .. import settings as settings_core
from ..executor import load_tools



class _LoginWorker(QThread):
    """Runs login request in background thread — keeps GUI responsive."""
    success = pyqtSignal(dict)   # result dict from backend
    error = pyqtSignal(str)

    def __init__(self, base_url, email, password):
        super().__init__()
        self._base_url = base_url
        self._email = email
        self._password = password

    def run(self):
        try:
            client = TriForceClient(self._base_url, timeout=15)
            result = client.login(self._email, self._password)
            self.success.emit(result)
        except Exception as e:
            self.error.emit(str(e))


class _ModelLoader(QThread):
    """Loads model list from backend in background."""
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
            models = [
                model_id for item in data.get("models", [])
                if (model_id := model_identifier(item))
            ]
            tier = data.get("tier", "?")
            self.loaded.emit(models, tier)
        except Exception as e:
            self.error.emit(str(e))


class _ToolLoader(QThread):
    """Discovers tool schemas only after the user requests them."""
    loaded = pyqtSignal(list)
    error = pyqtSignal(str)

    def __init__(self, client):
        super().__init__()
        self.client = client

    def run(self):
        try:
            self.loaded.emit(load_tools(self.client, force_refresh=True))
        except Exception as e:
            self.error.emit(str(e))


class _ModelProbe(QThread):
    """Runs a tiny no-fallback request and reports real end-to-end latency."""
    success = pyqtSignal(dict, float)
    error = pyqtSignal(str, float)

    def __init__(self, client, model):
        super().__init__()
        self.client = client
        self.model = model

    def run(self):
        import time
        started = time.monotonic()
        try:
            result = self.client.chat(
                message="Reply exactly: OK",
                model=self.model or None,
                temperature=0,
                max_tokens=8,
            )
            self.success.emit(result, time.monotonic() - started)
        except Exception as e:
            self.error.emit(str(e), time.monotonic() - started)


class SettingsWidget(QWidget):
    models_loaded = pyqtSignal(list)  # emitted with sorted model list
    selection_changed = pyqtSignal(str)  # base model
    tools_changed = pyqtSignal(str, object)  # (mode, selected names or None)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loader = None
        self._tool_loader = None
        self._probe = None
        self._models = []
        self._tools = []
        self._schema_widgets = {}
        self._team_model_combos = {}
        self._loading_settings = False
        self._settings_snapshot = None
        self._build_ui()
        self._load_current()
        self._settings_timer = QTimer(self)
        self._settings_timer.setInterval(1000)
        self._settings_timer.timeout.connect(self._refresh_external_settings)
        self._settings_timer.start()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setObjectName("SettingsScroll")
        scroll.setWidgetResizable(True)
        scroll.viewport().setObjectName("SettingsViewport")
        content = QWidget()
        content.setObjectName("SettingsContent")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(18, 10, 18, 18)
        layout.setSpacing(10)
        scroll.setWidget(content)
        outer.addWidget(scroll)

        # --- Login Group ---
        login_group = QGroupBox("Login")
        login_form = QFormLayout()
        login_form.setHorizontalSpacing(14)
        login_form.setVerticalSpacing(9)
        self.base_url_edit = QLineEdit(DEFAULT_BASE_URL)
        self.email_edit = QLineEdit()
        self.email_edit.setPlaceholderText("user@example.com")
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setPlaceholderText("Password")
        login_form.addRow("Base URL:", self.base_url_edit)
        login_form.addRow("E-Mail:", self.email_edit)
        login_form.addRow("Password:", self.password_edit)

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
        model_form.setHorizontalSpacing(14)
        model_form.setVerticalSpacing(9)

        # Model Dropdown (editable — user can type custom model too)
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.model_combo.lineEdit().setPlaceholderText("Modell auswählen oder ID eingeben...")
        self.model_combo.setMinimumWidth(500)
        self.model_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        # Refresh button
        refresh_btn = QPushButton("Modelle laden")
        refresh_btn.clicked.connect(self._load_models)

        self.model_status = QLabel("")
        self.model_status.setStyleSheet("color: #888; font-size: 11px;")

        model_form.addRow("Basismodell:", self.model_combo)

        timeout_spec = settings_core.REGISTRY["request_timeout"]
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(int(timeout_spec.minimum or 0), int(timeout_spec.maximum or 2_147_483_647))
        self.timeout_spin.setSuffix(" s")
        self.timeout_spin.setToolTip(timeout_spec.description)
        model_form.addRow("Request-Timeout:", self.timeout_spin)

        # Buttons row
        model_btn_row = QHBoxLayout()
        save_btn = QPushButton("Basismodell speichern")
        save_btn.clicked.connect(self._save_model_config)
        self.probe_btn = QPushButton("Modell testen")
        self.probe_btn.setToolTip("Kleine Direktanfrage; misst die reale End-to-End-Latenz dieses Modells")
        self.probe_btn.clicked.connect(self._test_model)
        model_btn_row.addWidget(refresh_btn)
        model_btn_row.addWidget(save_btn)
        model_btn_row.addWidget(self.probe_btn)
        model_btn_row.addWidget(self.model_status)
        model_btn_row.addStretch()
        model_form.addRow(model_btn_row)

        model_group.setLayout(model_form)
        layout.addWidget(model_group)

        # --- Agent Team Group ---
        team_group = QGroupBox("Agent-Team · RAM Multi-Agent Runtime")
        team_form = QFormLayout()
        team_form.setHorizontalSpacing(14)
        team_form.setVerticalSpacing(7)

        self.team_runtime_combo = QComboBox()
        team_mode_labels = {
            "off": "Aus — normaler Einzelagent",
            "auto": "Auto — Team nur für größere Coding-Aufgaben",
            "on": "An — Team bevorzugen",
        }
        for value in settings_core.REGISTRY["team_runtime_mode"].choice_list():
            self.team_runtime_combo.addItem(team_mode_labels.get(value, value), value)
        self.team_runtime_combo.setToolTip(settings_core.REGISTRY["team_runtime_mode"].description)
        team_form.addRow("Team-Runtime:", self.team_runtime_combo)

        team_labels = [
            ("team_research_model_1", "Recherche 1 · Primärquellen / aktuelle Docs"),
            ("team_research_model_2", "Recherche 2 · Best Practices / bewährte Architektur"),
            ("team_research_model_3", "Recherche 3 · Sicherheit / Zuverlässigkeit"),
            ("team_research_model_4", "Recherche 4 · Alternativen / ähnliche Systeme"),
            ("team_planner_model", "Planer · gemeinsamer Implementierungsplan"),
            ("team_coordinator_model", "Koordinator · Planprüfung / Laufsteuerung"),
            ("team_coder_model_1", "Coder 1 · konservativ / minimal"),
            ("team_coder_model_2", "Coder 2 · Architektur-first"),
            ("team_coder_model_3", "Coder 3 · Performance / Effizienz"),
            ("team_coder_model_4", "Coder 4 · Robustheit / Sicherheit"),
            ("team_merge_model", "Merge · Integration der besten Kandidaten"),
            ("team_finalizer_model", "Finalisierung · Reparatur / Abschlussprüfung"),
        ]
        for key, label in team_labels:
            combo = QComboBox()
            combo.setEditable(True)
            combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            combo.setMinimumWidth(500)
            combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            combo.addItem("off", "")
            combo.addItem("@primary", "@primary")
            combo.setToolTip(settings_core.REGISTRY[key].description)
            team_form.addRow(label + ":", combo)
            self._team_model_combos[key] = combo

        note = QLabel(
            "Leere/off Slots werden übersprungen. @primary verwendet das Basismodell. "
            "Dasselbe Modell darf beliebig oft eingesetzt werden. Recherche läuft read-only; "
            "Coder arbeiten in getrennten RAM-Workspaces. Bewertung erfolgt zuerst durch Tests und Messdaten."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #888; font-size: 11px;")
        team_form.addRow(note)

        team_save = QPushButton("Agent-Team speichern")
        team_save.clicked.connect(self._save_team_config)
        self.team_status = QLabel("")
        self.team_status.setStyleSheet("color: #888; font-size: 11px;")
        team_row = QHBoxLayout()
        team_row.addWidget(team_save)
        team_row.addWidget(self.team_status)
        team_row.addStretch()
        team_form.addRow(team_row)
        team_group.setLayout(team_form)
        layout.addWidget(team_group)

        # --- Permission Group ---
        permission_group = QGroupBox("Berechtigungen und Autopilot")
        permission_form = QFormLayout()
        approval_spec = settings_core.REGISTRY["approval_mode"]
        approval_labels = {
            "ask": "Manuell — jede Änderung bestätigen",
            "autopilot": "Autopilot — sichere Änderungen automatisch freigeben",
            "all": "Workspace-Auto — normale Änderungen automatisch freigeben",
        }
        self.approval_mode_combo = QComboBox()
        for value in approval_spec.choice_list():
            self.approval_mode_combo.addItem(approval_labels.get(value, value), value)
        self.approval_mode_combo.setMinimumWidth(420)
        self.approval_mode_combo.setToolTip(approval_spec.description)
        save_permissions_btn = QPushButton("Berechtigungen speichern")
        save_permissions_btn.clicked.connect(self._save_permission_config)
        self.permission_status = QLabel("Aktueller Modus wird aus state.json geladen")
        self.permission_status.setStyleSheet("color: #888; font-size: 11px;")
        row = QHBoxLayout()
        row.addWidget(save_permissions_btn)
        row.addWidget(self.permission_status)
        row.addStretch()
        permission_form.addRow("Freigabemodus:", self.approval_mode_combo)
        permission_form.addRow(row)
        permission_group.setLayout(permission_form)
        layout.addWidget(permission_group)

        # --- Tools Group ---
        tools_group = QGroupBox("Tools")
        tools_layout = QVBoxLayout()

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Mode:"))
        tool_mode_spec = settings_core.REGISTRY["tool_mode"]
        tool_mode_labels = {
            "off": "Off — chat only",
            "on_demand": "On demand — task-aware discovery",
            "always": "Always — every request",
        }
        self.tool_mode_combo = QComboBox()
        for value in tool_mode_spec.choice_list():
            self.tool_mode_combo.addItem(tool_mode_labels.get(value, value), value)
        self.tool_mode_combo.setToolTip(tool_mode_spec.description)
        self.tool_mode_combo.setMinimumWidth(260)
        self.tool_mode_combo.currentIndexChanged.connect(self._save_tool_mode_only)
        mode_row.addWidget(self.tool_mode_combo)
        self.tool_search = QLineEdit()
        self.tool_search.setPlaceholderText("Filter tools...")
        self.tool_search.textChanged.connect(self._filter_tools)
        mode_row.addWidget(self.tool_search, stretch=1)
        tools_layout.addLayout(mode_row)

        self.native_openrouter_checkbox = QCheckBox(
            "Enable native OpenRouter function calling (experimental)"
        )
        self.native_openrouter_checkbox.setToolTip(
            settings_core.REGISTRY["native_openrouter_tool_calling"].description
        )
        self.native_openrouter_checkbox.stateChanged.connect(self._save_native_openrouter_tool_calling)
        tools_layout.addWidget(self.native_openrouter_checkbox)

        self.tool_list = QListWidget()
        self.tool_list.setMinimumHeight(120)
        self.tool_list.setMaximumHeight(260)
        self.tool_list.setAlternatingRowColors(True)
        tools_layout.addWidget(self.tool_list)

        tool_btn_row = QHBoxLayout()
        self.load_tools_btn = QPushButton("Load / Refresh Tools")
        self.load_tools_btn.clicked.connect(self._load_tools)
        all_tools_btn = QPushButton("All")
        all_tools_btn.clicked.connect(lambda: self._set_all_tools(True))
        no_tools_btn = QPushButton("None")
        no_tools_btn.clicked.connect(lambda: self._set_all_tools(False))
        save_tools_btn = QPushButton("Save Tools")
        save_tools_btn.clicked.connect(self._save_tool_config)
        self.tool_status = QLabel("Not loaded — no startup request")
        self.tool_status.setStyleSheet("color: #888; font-size: 11px;")
        for button in (self.load_tools_btn, all_tools_btn, no_tools_btn, save_tools_btn):
            tool_btn_row.addWidget(button)
        tool_btn_row.addWidget(self.tool_status)
        tool_btn_row.addStretch()
        tools_layout.addLayout(tool_btn_row)

        tools_group.setLayout(tools_layout)
        layout.addWidget(tools_group, stretch=1)

        # --- Schema-driven settings not represented by the dedicated controls above ---
        handled = {
            "selected_model", "swarm_mode", "tool_mode",
            "enabled_tools", "request_timeout", "approval_mode",
            "native_openrouter_tool_calling",
        }
        additional = [
            spec for key, spec in sorted(settings_core.REGISTRY.items(), key=lambda item: (item[1].group, item[0]))
            if key not in handled and not key.startswith("team_") and not spec.sensitive
        ]
        if additional:
            schema_group = QGroupBox("Weitere Einstellungen")
            schema_form = QFormLayout()
            schema_form.setHorizontalSpacing(14)
            schema_form.setVerticalSpacing(9)
            for spec in additional:
                widget = self._create_schema_widget(spec)
                widget.setToolTip(spec.description)
                label = f"{spec.key}{' ⚠' if spec.security_impact else ''}:"
                schema_form.addRow(label, widget)
                self._schema_widgets[spec.key] = widget
            schema_save = QPushButton("Weitere Einstellungen speichern")
            schema_save.clicked.connect(self._save_schema_settings)
            self.schema_status = QLabel("")
            self.schema_status.setStyleSheet("color: #888; font-size: 11px;")
            schema_row = QHBoxLayout()
            schema_row.addWidget(schema_save)
            schema_row.addWidget(self.schema_status)
            schema_row.addStretch()
            schema_form.addRow(schema_row)
            schema_group.setLayout(schema_form)
            layout.addWidget(schema_group)


    def _create_schema_widget(self, spec):
        if spec.type == "enum":
            widget = QComboBox()
            for value in spec.choice_list():
                widget.addItem(value, value)
            return widget
        if spec.type == "int":
            widget = QSpinBox()
            widget.setRange(
                int(spec.minimum if spec.minimum is not None else -2_147_483_648),
                int(spec.maximum if spec.maximum is not None else 2_147_483_647),
            )
            return widget
        if spec.type == "bool":
            widget = QComboBox()
            widget.addItem("false", False)
            widget.addItem("true", True)
            return widget
        widget = QLineEdit()
        if spec.type == "list":
            widget.setPlaceholderText("all, none, or comma-separated values")
        return widget

    def _schema_widget_value(self, key: str):
        spec = settings_core.REGISTRY[key]
        widget = self._schema_widgets[key]
        if spec.type == "enum":
            return widget.currentData()
        if spec.type == "int":
            return widget.value()
        if spec.type == "bool":
            return widget.currentData()
        return widget.text()

    def _set_schema_widget_value(self, key: str, value):
        spec = settings_core.REGISTRY[key]
        widget = self._schema_widgets[key]
        if spec.type in {"enum", "bool"}:
            index = widget.findData(value)
            if index >= 0:
                widget.setCurrentIndex(index)
        elif spec.type == "int":
            widget.setValue(int(value if value is not None else spec.default or 0))
        elif spec.type == "list":
            if value is None:
                widget.setText("all")
            elif value == []:
                widget.setText("none")
            else:
                widget.setText(",".join(str(item) for item in value))
        else:
            widget.setText("" if value is None else str(value))

    def _save_schema_settings(self):
        if self._loading_settings:
            return
        proposed = {}
        try:
            for key in self._schema_widgets:
                proposed[key] = settings_core.coerce(key, self._schema_widget_value(key))
        except settings_core.SettingsError as exc:
            self.schema_status.setText(f"Invalid setting: {exc}")
            self.schema_status.setStyleSheet("color: #ff6b6b; font-size: 11px;")
            return

        state = get_state()
        security_changes = [
            key for key, value in proposed.items()
            if settings_core.REGISTRY[key].security_impact
            and state.get(key, settings_core.REGISTRY[key].default) != value
        ]
        if security_changes:
            reply = QMessageBox.question(
                self,
                "Security setting change",
                "These settings change a security boundary and require explicit confirmation:\n"
                + "\n".join(f"- {key}" for key in security_changes)
                + "\n\nApply these changes?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                self.schema_status.setText("Not changed")
                return

        try:
            settings_core.STORE.update(**proposed)
        except settings_core.SettingsError as exc:
            self.schema_status.setText(f"Invalid setting: {exc}")
            self.schema_status.setStyleSheet("color: #ff6b6b; font-size: 11px;")
            return
        current = get_state()
        self._settings_snapshot = self._state_signature(current)
        self.schema_status.setText(f"Saved · {len(proposed)} settings")
        self.schema_status.setStyleSheet("color: #00ff88; font-size: 11px;")


    @staticmethod
    def _state_signature(state: dict) -> tuple:
        """Stable signature of canonical settings; excludes migration metadata."""
        return tuple((key, repr(state.get(key, spec.default))) for key, spec in sorted(settings_core.REGISTRY.items()))

    def _apply_state_to_widgets(self, state: dict, *, emit_changes: bool = False):
        """Apply persisted state without accidentally writing it back through UI signals."""
        previous = self._settings_snapshot
        self._loading_settings = True
        try:
            self.model_combo.setCurrentText(str(state.get("selected_model") or ""))
            mode_idx = self.tool_mode_combo.findData(state.get("tool_mode", settings_core.REGISTRY["tool_mode"].default))
            if mode_idx >= 0:
                self.tool_mode_combo.setCurrentIndex(mode_idx)
            self.native_openrouter_checkbox.setChecked(
                bool(state.get("native_openrouter_tool_calling", False))
            )
            self.timeout_spin.setValue(int(state.get("request_timeout", settings_core.REGISTRY["request_timeout"].default)))
            approval_idx = self.approval_mode_combo.findData(state.get("approval_mode", settings_core.REGISTRY["approval_mode"].default))
            if approval_idx >= 0:
                self.approval_mode_combo.setCurrentIndex(approval_idx)
            team_idx = self.team_runtime_combo.findData(state.get("team_runtime_mode", settings_core.REGISTRY["team_runtime_mode"].default))
            if team_idx >= 0:
                self.team_runtime_combo.setCurrentIndex(team_idx)
            for key, combo in self._team_model_combos.items():
                value = str(state.get(key, settings_core.REGISTRY[key].default) or "")
                combo.setCurrentText(value or "off")
            for key, spec in self._schema_widgets.items():
                self._set_schema_widget_value(key, state.get(key, settings_core.REGISTRY[key].default))
        finally:
            self._loading_settings = False

        self._settings_snapshot = self._state_signature(state)
        if emit_changes and previous is not None:
            self.selection_changed.emit(str(state.get("selected_model") or ""))
            self.tools_changed.emit(
                str(state.get("tool_mode", settings_core.REGISTRY["tool_mode"].default)),
                state.get("enabled_tools"),
            )

    def _refresh_external_settings(self):
        """Make CLI/REPL changes visible in a running GUI without a restart."""
        state = get_state()
        signature = self._state_signature(state)
        if signature != self._settings_snapshot:
            self._apply_state_to_widgets(state, emit_changes=True)

    def _load_current(self):
        # Session
        try:
            session = load_session()
            self.base_url_edit.setText(session.base_url)
            self.email_edit.setText(session.user_id)
            client = TriForceClient(session.base_url, token=session.token)
            if client.is_token_expired():
                self.status_label.setText("Session expired — login again")
                self.status_label.setStyleSheet("color: #ffb020;")
            else:
                self.status_label.setText(f"Logged in as {session.user_id} ({session.tier})")
                self.status_label.setStyleSheet("color: #00d4ff;")
                # Auto-load models on startup if logged in
                self._load_models()
        except Exception:
            self.status_label.setText("Not logged in")
            self.status_label.setStyleSheet("color: #ff6b6b;")

        # State
        self._apply_state_to_widgets(get_state())

    def _load_models(self):
        """Load model list from backend."""
        try:
            session = load_session()
            client = TriForceClient(session.base_url, token=session.token, timeout=10)
        except Exception:
            self.model_status.setText("Not logged in")
            self.model_status.setStyleSheet("color: #ff6b6b; font-size: 11px;")
            return

        self.model_status.setText("Modelle werden geladen...")
        self.model_status.setStyleSheet("color: #00d4ff; font-size: 11px;")

        self._loader = _ModelLoader(client)
        self._loader.loaded.connect(self._on_models_loaded)
        self._loader.error.connect(self._on_models_error)
        self._loader.start()

    def _on_models_loaded(self, models: list, tier: str):
        self._models = sorted(models)

        # Save current selection
        cur_model = self.model_combo.currentText()

        # Populate dropdowns
        self.model_combo.clear()
        self.model_combo.addItem("")     # empty = backend default

        for m in self._models:
            self.model_combo.addItem(m)
        state = get_state()
        for key, combo in self._team_model_combos.items():
            current = combo.currentText().strip()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("off", "")
            combo.addItem("@primary", "@primary")
            for m in self._models:
                combo.addItem(m, m)
            desired = current or str(state.get(key, settings_core.REGISTRY[key].default) or "off")
            combo.setCurrentText(desired)
            combo.blockSignals(False)

        # Restore selection
        if cur_model:
            self.model_combo.setCurrentText(cur_model)

        self.model_status.setText(f"{len(models)} Modelle ({tier})")
        self.model_status.setStyleSheet("color: #00ff88; font-size: 11px;")
        self.models_loaded.emit(self._models)

    def _on_models_error(self, err: str):
        self.model_status.setText(f"Error: {err[:60]}")
        self.model_status.setStyleSheet("color: #ff6b6b; font-size: 11px;")

    def _test_model(self):
        model = self.model_combo.currentText().strip()
        try:
            session = load_session()
            timeout = min(30, self.timeout_spin.value())
            client = TriForceClient(session.base_url, token=session.token, timeout=timeout)
        except Exception as e:
            self.model_status.setText(f"Test unavailable: {e}")
            return
        self.probe_btn.setEnabled(False)
        self.model_status.setText(f"Teste {model or 'Backend-Standard'}...")
        self.model_status.setStyleSheet("color: #00d4ff; font-size: 11px;")
        self._probe = _ModelProbe(client, model)
        self._probe.success.connect(lambda result, elapsed: self._on_probe_success(model, result, elapsed))
        self._probe.error.connect(self._on_probe_error)
        self._probe.start()

    def _on_probe_success(self, requested: str, result: dict, elapsed: float):
        self.probe_btn.setEnabled(True)
        actual = result.get("model") or result.get("provider") or "unknown"
        route = actual if not requested or actual == requested else f"{requested} → {actual}"
        color = "#00ff88" if elapsed < 10 else "#ffb020"
        self.model_status.setText(f"Test OK · {elapsed:.1f}s · {route}")
        self.model_status.setStyleSheet(f"color: {color}; font-size: 11px;")

    def _on_probe_error(self, err: str, elapsed: float):
        self.probe_btn.setEnabled(True)
        self.model_status.setText(f"Test failed after {elapsed:.1f}s: {err[:80]}")
        self.model_status.setStyleSheet("color: #ff6b6b; font-size: 11px;")

    def _load_tools(self):
        try:
            session = load_session()
            client = TriForceClient(session.base_url, token=session.token, timeout=12)
        except Exception as e:
            self.tool_status.setText(f"Not logged in: {e}")
            return
        self.load_tools_btn.setEnabled(False)
        self.tool_status.setText("Loading tools...")
        self.tool_status.setStyleSheet("color: #00d4ff; font-size: 11px;")
        self._tool_loader = _ToolLoader(client)
        self._tool_loader.loaded.connect(self._on_tools_loaded)
        self._tool_loader.error.connect(self._on_tools_error)
        self._tool_loader.start()

    def _on_tools_loaded(self, tools: list):
        self.load_tools_btn.setEnabled(True)
        self._tools = sorted(tools, key=lambda tool: tool.get("name", ""))
        saved = get_state().get("enabled_tools")
        selected = None if saved is None else set(saved)
        self.tool_list.clear()
        for tool in self._tools:
            name = tool.get("name", "?")
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            checked = selected is None or name in selected
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            item.setToolTip(tool.get("description", ""))
            self.tool_list.addItem(item)
        self._filter_tools(self.tool_search.text())
        self.tool_status.setText(f"{len(self._tools)} available")
        self.tool_status.setStyleSheet("color: #00ff88; font-size: 11px;")

    def _on_tools_error(self, err: str):
        self.load_tools_btn.setEnabled(True)
        self.tool_status.setText(f"Error: {err[:80]}")
        self.tool_status.setStyleSheet("color: #ff6b6b; font-size: 11px;")

    def _filter_tools(self, query: str):
        query = query.strip().lower()
        for row in range(self.tool_list.count()):
            item = self.tool_list.item(row)
            item.setHidden(bool(query) and query not in item.text().lower()
                           and query not in item.toolTip().lower())

    def _set_all_tools(self, checked: bool):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for row in range(self.tool_list.count()):
            self.tool_list.item(row).setCheckState(state)

    def _save_tool_mode_only(self):
        if self._loading_settings:
            return
        mode = self.tool_mode_combo.currentData() or settings_core.REGISTRY["tool_mode"].default
        set_tool_mode(mode)
        self.tool_status.setText(f"Mode saved · {mode}")
        self.tool_status.setStyleSheet("color: #00ff88; font-size: 11px;")
        state = get_state()
        self._settings_snapshot = self._state_signature(state)
        self.tools_changed.emit(mode, state.get("enabled_tools"))

    def _save_native_openrouter_tool_calling(self):
        if self._loading_settings:
            return
        enabled = self.native_openrouter_checkbox.isChecked()
        set_native_openrouter_tool_calling(enabled)
        self.tool_status.setText(
            "Native OpenRouter tools enabled" if enabled else "Text tool protocol enabled"
        )
        self.tool_status.setStyleSheet("color: #00ff88; font-size: 11px;")
        self._settings_snapshot = self._state_signature(get_state())

    def _save_tool_config(self):
        mode = self.tool_mode_combo.currentData() or "on_demand"
        names = [
            self.tool_list.item(row).text()
            for row in range(self.tool_list.count())
            if self.tool_list.item(row).checkState() == Qt.CheckState.Checked
        ]
        # None means dynamic "all tools", so future capabilities are picked up too.
        # Preserve the previous value before discovery rather than accidentally saving [].
        if self.tool_list.count():
            selected = None if len(names) == self.tool_list.count() else names
        else:
            selected = get_state().get("enabled_tools")
        set_tool_mode(mode)
        set_enabled_tools(selected)
        self.tool_status.setText(
            f"Saved · {'all' if selected is None else len(selected)} enabled"
        )
        self.tool_status.setStyleSheet("color: #00ff88; font-size: 11px;")
        self._settings_snapshot = self._state_signature(get_state())
        self.tools_changed.emit(mode, selected)


    def _save_team_config(self):
        if self._loading_settings:
            return
        proposed = {"team_runtime_mode": self.team_runtime_combo.currentData() or "auto"}
        for key, combo in self._team_model_combos.items():
            text = combo.currentText().strip()
            proposed[key] = "" if text.lower() in {"off", "none", "disabled"} else text
        try:
            settings_core.STORE.update(**proposed)
            from ..team_runtime import config_from_state
            config = config_from_state(get_state())
            errors = config.validate()
        except settings_core.SettingsError as exc:
            self.team_status.setText(f"Ungültige Einstellung: {exc}")
            self.team_status.setStyleSheet("color: #ff6b6b; font-size: 11px;")
            return
        if errors:
            self.team_status.setText("Gespeichert mit Hinweis · " + "; ".join(errors))
            self.team_status.setStyleSheet("color: #ffb020; font-size: 11px;")
        else:
            self.team_status.setText(
                f"Gespeichert · {len(config.research)} Recherche · {len(config.coders)} Coder · {config.active_count} Rollen"
            )
            self.team_status.setStyleSheet("color: #00ff88; font-size: 11px;")
        self._settings_snapshot = self._state_signature(get_state())

    def _save_permission_config(self):
        if self._loading_settings:
            return
        mode = self.approval_mode_combo.currentData() or settings_core.REGISTRY["approval_mode"].default
        set_approval_mode(mode)
        self.permission_status.setText(f"Saved · {mode}")
        self.permission_status.setStyleSheet("color: #00ff88; font-size: 11px;")
        self._settings_snapshot = self._state_signature(get_state())

    def _do_login(self):
        base_url = self.base_url_edit.text().strip()
        email = self.email_edit.text().strip()
        password = self.password_edit.text()
        if not email or not password:
            QMessageBox.warning(self, "Login", "Enter email and password.")
            return
        self.login_btn.setEnabled(False)
        self.status_label.setText("Logging in...")
        self.status_label.setStyleSheet("color: #888; font-size: 11px;")
        self._login_worker = _LoginWorker(base_url, email, password)
        self._login_worker.success.connect(lambda r: self._on_login_success(r, base_url, email))
        self._login_worker.error.connect(self._on_login_error)
        self._login_worker.start()

    def _on_login_success(self, result, base_url, email):
        self.login_btn.setEnabled(True)
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
        self.status_label.setText(f"Logged in as {session.user_id} ({session.tier})")
        self.status_label.setStyleSheet("color: #00d4ff;")
        self._load_models()

    def _on_login_error(self, msg):
        self.login_btn.setEnabled(True)
        self.status_label.setText("Login failed")
        self.status_label.setStyleSheet("color: #ff6b6b; font-size: 11px;")
        QMessageBox.critical(self, "Login failed", msg)

    def _do_logout(self):
        delete_session()
        self.status_label.setText("Not logged in")
        self.status_label.setStyleSheet("color: #ff6b6b;")
        self.model_combo.clear()
        self._models = []
        self.tool_list.clear()
        self._tools = []

    def _save_model_config(self):
        model = self.model_combo.currentText().strip()
        set_model(model)
        set_request_timeout(self.timeout_spin.value())
        self.model_status.setText("Gespeichert.")
        self.model_status.setStyleSheet("color: #00ff88; font-size: 11px;")
        self._settings_snapshot = self._state_signature(get_state())
        self.selection_changed.emit(model)

    def get_current_model(self) -> str:
        return self.model_combo.currentText().strip()

    def get_tool_mode(self) -> str:
        return self.tool_mode_combo.currentData() or "on_demand"

    def get_enabled_tool_names(self):
        return get_state().get("enabled_tools")

    def get_request_timeout(self) -> int:
        return self.timeout_spin.value()
