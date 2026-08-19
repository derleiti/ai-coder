"""Settings tab — Login, model dropdown, fallback dropdown, swarm."""
from __future__ import annotations
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLineEdit, QPushButton, QComboBox, QLabel, QGroupBox, QMessageBox,
    QListWidget, QListWidgetItem, QSpinBox, QSizePolicy, QScrollArea,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal

from ..config import DEFAULT_BASE_URL, Session, load_session, save_session, delete_session
from ..session_state import (
    SWARM_MODES, get_state, set_model, set_fallback, set_swarm,
    set_tool_mode, set_enabled_tools, set_request_timeout,
    set_approval_mode,
)
from ..client import TriForceClient, model_identifier
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
    selection_changed = pyqtSignal(str, str)  # (model, fallback)
    tools_changed = pyqtSignal(str, object)  # (mode, selected names or None)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loader = None
        self._tool_loader = None
        self._probe = None
        self._models = []
        self._tools = []
        self._build_ui()
        self._load_current()

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
        model_group = QGroupBox("Model Configuration")
        model_form = QFormLayout()
        model_form.setHorizontalSpacing(14)
        model_form.setVerticalSpacing(9)

        # Model Dropdown (editable — user can type custom model too)
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.model_combo.lineEdit().setPlaceholderText("Select or enter model...")
        self.model_combo.setMinimumWidth(500)
        self.model_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        # Fallback Dropdown
        self.fallback_combo = QComboBox()
        self.fallback_combo.setEditable(True)
        self.fallback_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.fallback_combo.lineEdit().setPlaceholderText("Select fallback...")
        self.fallback_combo.setMinimumWidth(500)
        self.fallback_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        # Refresh button
        refresh_btn = QPushButton("Load Models")
        refresh_btn.clicked.connect(self._load_models)

        self.model_status = QLabel("")
        self.model_status.setStyleSheet("color: #888; font-size: 11px;")

        model_form.addRow("Model:", self.model_combo)
        model_form.addRow("Fallback:", self.fallback_combo)

        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(10, 300)
        self.timeout_spin.setSuffix(" s")
        self.timeout_spin.setToolTip("Maximum wait per model attempt. A configured fallback gets its own attempt.")
        model_form.addRow("Timeout:", self.timeout_spin)

        # Swarm
        self.swarm_combo = QComboBox()
        self.swarm_combo.addItems(sorted(SWARM_MODES))
        model_form.addRow("Swarm:", self.swarm_combo)

        # Buttons row
        model_btn_row = QHBoxLayout()
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self._save_model_config)
        self.probe_btn = QPushButton("Test Model")
        self.probe_btn.setToolTip("Tiny request without fallback; measures the selected model itself")
        self.probe_btn.clicked.connect(self._test_model)
        model_btn_row.addWidget(refresh_btn)
        model_btn_row.addWidget(save_btn)
        model_btn_row.addWidget(self.probe_btn)
        model_btn_row.addWidget(self.model_status)
        model_btn_row.addStretch()
        model_form.addRow(model_btn_row)

        model_group.setLayout(model_form)
        layout.addWidget(model_group)

        # --- Permission Group ---
        permission_group = QGroupBox("Berechtigungen und Autopilot")
        permission_form = QFormLayout()
        self.approval_mode_combo = QComboBox()
        self.approval_mode_combo.addItem("Manuell — jede Änderung bestätigen", "ask")
        self.approval_mode_combo.addItem("Autopilot — normale Befehle automatisch freigeben", "autopilot")
        self.approval_mode_combo.addItem("Workspace-Auto — Änderungen automatisch, Löschen weiter bestätigen", "all")
        self.approval_mode_combo.setMinimumWidth(420)
        self.approval_mode_combo.setToolTip(
            "Freigaben gelten nur für Workspace-Mutationen. Root-, sudo-, Service- und Shell-Aktionen sind im Coding-only-Profil deaktiviert."
        )
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
        self.approval_mode_combo.currentIndexChanged.connect(self._save_permission_config)
        permission_group.setLayout(permission_form)
        layout.addWidget(permission_group)

        # --- Tools Group ---
        tools_group = QGroupBox("Tools")
        tools_layout = QVBoxLayout()

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Mode:"))
        self.tool_mode_combo = QComboBox()
        self.tool_mode_combo.addItem("Off — chat only", "off")
        self.tool_mode_combo.addItem("On demand — smart workspace detection", "on_demand")
        self.tool_mode_combo.addItem("Always — every request", "always")
        self.tool_mode_combo.setMinimumWidth(260)
        self.tool_mode_combo.currentIndexChanged.connect(self._save_tool_mode_only)
        mode_row.addWidget(self.tool_mode_combo)
        self.tool_search = QLineEdit()
        self.tool_search.setPlaceholderText("Filter tools...")
        self.tool_search.textChanged.connect(self._filter_tools)
        mode_row.addWidget(self.tool_search, stretch=1)
        tools_layout.addLayout(mode_row)

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
        state = get_state()
        if state.get("selected_model"):
            self.model_combo.setCurrentText(state["selected_model"])
        if state.get("fallback_model"):
            self.fallback_combo.setCurrentText(state["fallback_model"])
        idx = self.swarm_combo.findText(state.get("swarm_mode", "off"))
        if idx >= 0:
            self.swarm_combo.setCurrentIndex(idx)
        mode_idx = self.tool_mode_combo.findData(state.get("tool_mode", "on_demand"))
        if mode_idx >= 0:
            self.tool_mode_combo.setCurrentIndex(mode_idx)
        self.timeout_spin.setValue(int(state.get("request_timeout", 300)))
        approval_idx = self.approval_mode_combo.findData(state.get("approval_mode", "ask"))
        if approval_idx >= 0:
            self.approval_mode_combo.setCurrentIndex(approval_idx)

    def _load_models(self):
        """Load model list from backend."""
        try:
            session = load_session()
            client = TriForceClient(session.base_url, token=session.token, timeout=10)
        except Exception:
            self.model_status.setText("Not logged in")
            self.model_status.setStyleSheet("color: #ff6b6b; font-size: 11px;")
            return

        self.model_status.setText("Loading models...")
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
        self.model_combo.addItem("")     # empty = backend default
        self.fallback_combo.addItem("")  # empty = no fallback

        for m in self._models:
            self.model_combo.addItem(m)
            self.fallback_combo.addItem(m)

        # Restore selection
        if cur_model:
            self.model_combo.setCurrentText(cur_model)
        if cur_fallback:
            self.fallback_combo.setCurrentText(cur_fallback)

        self.model_status.setText(f"{len(models)} models ({tier})")
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
        self.model_status.setText(f"Testing {model or 'backend default'}...")
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
        mode = self.tool_mode_combo.currentData() or "on_demand"
        set_tool_mode(mode)
        self.tool_status.setText(f"Mode saved · {mode}")
        self.tool_status.setStyleSheet("color: #00ff88; font-size: 11px;")
        self.tools_changed.emit(mode, get_state().get("enabled_tools"))

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
        self.tools_changed.emit(mode, selected)


    def _save_permission_config(self):
        mode = self.approval_mode_combo.currentData() or "ask"
        set_approval_mode(mode)
        self.permission_status.setText(f"Saved · {mode}")
        self.permission_status.setStyleSheet("color: #00ff88; font-size: 11px;")

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
        self.fallback_combo.clear()
        self._models = []
        self.tool_list.clear()
        self._tools = []

    def _save_model_config(self):
        model = self.model_combo.currentText().strip()
        fallback = self.fallback_combo.currentText().strip()
        swarm = self.swarm_combo.currentText()
        set_model(model)
        set_fallback(fallback)
        set_swarm(swarm)
        set_request_timeout(self.timeout_spin.value())
        self.model_status.setText("Saved.")
        self.model_status.setStyleSheet("color: #00ff88; font-size: 11px;")
        self.selection_changed.emit(model, fallback)

    def get_current_model(self) -> str:
        return self.model_combo.currentText().strip()

    def get_current_fallback(self) -> str:
        return self.fallback_combo.currentText().strip()

    def get_tool_mode(self) -> str:
        return self.tool_mode_combo.currentData() or "on_demand"

    def get_enabled_tool_names(self):
        return get_state().get("enabled_tools")

    def get_request_timeout(self) -> int:
        return self.timeout_spin.value()
