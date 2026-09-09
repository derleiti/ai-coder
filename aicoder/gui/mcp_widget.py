"""Production MCP server manager backed by the canonical MCP service."""
from __future__ import annotations

import shlex

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFormLayout, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QMessageBox, QPushButton,
    QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..mcp_registry import MCPServerConfig
from ..mcp_service import (
    authentication_status, authorize_and_save_server, authorize_oauth, get_server,
    list_servers, remove_server, required_secret_field, save_server, server_tools,
    set_server_enabled, test_candidate, test_server,
)


class MCPServersWidget(QWidget):
    """Edit external MCP profiles without ever reading secrets back into the UI."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._selected_name = ""
        self._connection_state: dict[str, bool] = {}
        self._build()
        self.refresh()

    @staticmethod
    def _csv(text: str) -> list[str]:
        return [part.strip() for part in str(text or "").split(",") if part.strip()]

    def _build(self) -> None:
        root = QHBoxLayout(self)
        self.servers = QListWidget()
        self.servers.setMinimumWidth(240)
        self.servers.currentItemChanged.connect(self._selection_changed)
        root.addWidget(self.servers, 1)

        right = QVBoxLayout()
        form = QFormLayout()
        self.name = QLineEdit()
        self.enabled = QCheckBox("Enabled")
        self.enabled.setChecked(True)
        self.transport = QComboBox()
        self.transport.addItems(["streamable-http", "stdio"])
        self.url = QLineEdit()
        self.command = QLineEdit()
        self.arguments = QLineEdit()
        self.arguments.setPlaceholderText("shell-like argv, e.g. --stdio ./server.py")
        self.env_names = QLineEdit()
        self.env_names.setPlaceholderText("PATH, HOME, MY_NONSECRET_SETTING")
        self.timeout = QSpinBox()
        self.timeout.setRange(1, 300)
        self.timeout.setValue(30)
        self.trust = QComboBox()
        self.trust.addItems(["untrusted", "trusted"])
        self.allow_tools = QLineEdit()
        self.deny_tools = QLineEdit()
        self.capability_tags = QLineEdit()

        form.addRow("Name", self.name)
        form.addRow("Enabled", self.enabled)
        form.addRow("Transport", self.transport)
        form.addRow("URL", self.url)
        form.addRow("stdio Command", self.command)
        form.addRow("stdio Arguments", self.arguments)
        form.addRow("Environment allowlist", self.env_names)
        form.addRow("Timeout (s)", self.timeout)
        form.addRow("Trust", self.trust)
        form.addRow("Allow tools", self.allow_tools)
        form.addRow("Deny tools", self.deny_tools)
        form.addRow("Capability tags", self.capability_tags)
        connection_box = QGroupBox("Connection & Policy")
        connection_box.setLayout(form)
        right.addWidget(connection_box)

        auth_form = QFormLayout()
        self.auth = QComboBox()
        self.auth.addItems(["none", "api-key", "bearer", "basic", "oauth2", "custom-header"])
        self.auth.currentTextChanged.connect(self._auth_changed)
        self.username = QLineEdit()
        self.auth_header = QLineEdit("X-API-Key")
        self.secret = QLineEdit()
        self.secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.secret.setPlaceholderText("Stored in OS keyring; never loaded back")
        self.oauth_client_id = QLineEdit()
        self.oauth_authorization_url = QLineEdit()
        self.oauth_token_url = QLineEdit()
        self.oauth_scopes = QLineEdit()
        self.oauth_client_secret = QLineEdit()
        self.oauth_client_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.oauth_client_secret.setPlaceholderText("Optional; stored in OS keyring")
        self.oauth_authorize = QPushButton("Authorize OAuth")
        self.oauth_authorize.clicked.connect(self.authorize)

        auth_form.addRow("Authentication", self.auth)
        auth_form.addRow("Username", self.username)
        auth_form.addRow("Header name", self.auth_header)
        auth_form.addRow("Credential", self.secret)
        auth_form.addRow("OAuth Client ID", self.oauth_client_id)
        auth_form.addRow("Authorization URL", self.oauth_authorization_url)
        auth_form.addRow("Token URL", self.oauth_token_url)
        auth_form.addRow("Scopes", self.oauth_scopes)
        auth_form.addRow("OAuth Client Secret", self.oauth_client_secret)
        auth_form.addRow("", self.oauth_authorize)
        auth_box = QGroupBox("Authentication")
        auth_box.setLayout(auth_form)
        right.addWidget(auth_box)

        buttons = QGridLayout()
        self.new_button = QPushButton("New")
        self.save_button = QPushButton("Save")
        self.test_button = QPushButton("Test Connection")
        self.refresh_button = QPushButton("Refresh")
        self.remove_button = QPushButton("Remove")
        self.toggle_button = QPushButton("Enable / Disable")
        self.tools_button = QPushButton("Show Tools")
        actions = [
            (self.new_button, self.clear), (self.save_button, self.save),
            (self.test_button, self.test), (self.refresh_button, self.refresh),
            (self.remove_button, self.remove), (self.toggle_button, self.toggle_enabled),
            (self.tools_button, self.tools),
        ]
        for index, (button, callback) in enumerate(actions):
            button.clicked.connect(callback)
            buttons.addWidget(button, index // 4, index % 4)
        right.addLayout(buttons)

        self.status = QLabel("Secrets are stored only in the operating-system keyring.")
        self.status.setWordWrap(True)
        right.addWidget(self.status)
        right.addStretch(1)
        root.addLayout(right, 3)

        self.transport.currentTextChanged.connect(self._transport_changed)
        self._transport_changed(self.transport.currentText())
        self._auth_changed(self.auth.currentText())

    def _selection_changed(self, current, _previous) -> None:
        if current is None:
            return
        self._load(str(current.data(Qt.ItemDataRole.UserRole) or ""))

    def _set_editor_enabled(self, enabled: bool) -> None:
        for widget in (
            self.name, self.enabled, self.transport, self.url, self.command,
            self.arguments, self.env_names, self.timeout, self.trust,
            self.allow_tools, self.deny_tools, self.capability_tags, self.auth,
            self.username, self.auth_header, self.secret, self.oauth_client_id,
            self.oauth_authorization_url, self.oauth_token_url, self.oauth_scopes,
            self.oauth_client_secret, self.oauth_authorize,
        ):
            widget.setEnabled(enabled)
        self.save_button.setEnabled(enabled)
        self.remove_button.setEnabled(enabled)
        self.toggle_button.setEnabled(enabled)

    def _transport_changed(self, transport: str) -> None:
        stdio = transport == "stdio"
        self.command.setVisible(stdio)
        self.arguments.setVisible(stdio)
        self.env_names.setVisible(stdio)
        self.url.setVisible(not stdio)
        self.auth.setEnabled(not stdio and self.name.isEnabled())
        if stdio:
            self.auth.setCurrentText("none")
        self._auth_changed(self.auth.currentText())

    def _auth_changed(self, mode: str) -> None:
        http = self.transport.currentText() == "streamable-http"
        editable = self.name.isEnabled() and http
        self.username.setVisible(mode == "basic")
        self.auth_header.setVisible(mode in {"api-key", "custom-header"})
        self.secret.setVisible(mode in {"api-key", "bearer", "basic", "custom-header"})
        oauth = mode == "oauth2"
        for widget in (
            self.oauth_client_id, self.oauth_authorization_url, self.oauth_token_url,
            self.oauth_scopes, self.oauth_client_secret, self.oauth_authorize,
        ):
            widget.setVisible(oauth)
            widget.setEnabled(editable and oauth)
        if mode == "api-key" and not self.auth_header.text().strip():
            self.auth_header.setText("X-API-Key")

    def refresh(self) -> None:
        selected = self._selected_name or self.name.text().strip()
        self.servers.blockSignals(True)
        self.servers.clear()
        target_item = None
        for row in list_servers():
            name = str(row.get("name") or "")
            builtin = bool(row.get("builtin"))
            enabled = bool(row.get("enabled"))
            transport = str(row.get("transport") or "")
            if builtin:
                if name in self._connection_state:
                    state = "connected" if self._connection_state[name] else "failed"
                    marker = "●" if self._connection_state[name] else "!"
                else:
                    state = "builtin"
                    marker = "●"
            elif not enabled:
                state = "disabled"
                marker = "○"
            elif name in self._connection_state:
                state = "connected" if self._connection_state[name] else "failed"
                marker = "●" if self._connection_state[name] else "!"
            else:
                state = "enabled"
                marker = "●"
            from PyQt6.QtWidgets import QListWidgetItem
            item = QListWidgetItem(f"{marker} {name}    {transport}    {state}")
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.servers.addItem(item)
            if name == selected:
                target_item = item
        self.servers.blockSignals(False)
        if target_item is not None:
            self.servers.setCurrentItem(target_item)
        elif not self._selected_name:
            self.clear()

    def clear(self) -> None:
        self._selected_name = ""
        self.servers.clearSelection()
        self._set_editor_enabled(True)
        self.name.setEnabled(True)
        self.name.clear()
        self.enabled.setChecked(True)
        self.transport.setCurrentText("streamable-http")
        self.url.clear()
        self.command.clear()
        self.arguments.clear()
        self.env_names.clear()
        self.timeout.setValue(30)
        self.trust.setCurrentText("untrusted")
        self.allow_tools.clear()
        self.deny_tools.clear()
        self.capability_tags.clear()
        self.auth.setCurrentText("none")
        self.username.clear()
        self.auth_header.setText("X-API-Key")
        self.secret.clear()
        self.oauth_client_id.clear()
        self.oauth_authorization_url.clear()
        self.oauth_token_url.clear()
        self.oauth_scopes.clear()
        self.oauth_client_secret.clear()
        self.status.setText("New MCP server · default trust: untrusted")
        self._transport_changed(self.transport.currentText())

    def _load(self, name: str) -> None:
        if not name:
            return
        self._selected_name = name
        self.secret.clear()
        self.oauth_client_secret.clear()
        if name == "triforce":
            self.clear()
            self._selected_name = "triforce"
            self.name.setText("triforce")
            self.transport.setCurrentText("streamable-http")
            self._set_editor_enabled(False)
            self.test_button.setEnabled(True)
            self.tools_button.setEnabled(True)
            self.refresh_button.setEnabled(True)
            self.new_button.setEnabled(True)
            self.status.setText("Built-in TriForce MCP · read-only here · authentication remains managed by AICoder login/RBAC/recovery.")
            return
        self._set_editor_enabled(True)
        config = get_server(name)
        if config is None:
            return
        self.name.setText(config.name)
        self.name.setEnabled(False)  # identity is stable; use New to create another profile
        self.enabled.setChecked(config.enabled)
        self.transport.setCurrentText(config.transport)
        self.url.setText(config.url)
        self.command.setText(config.command)
        self.arguments.setText(shlex.join(config.args))
        self.env_names.setText(", ".join(config.env_names))
        self.timeout.setValue(config.timeout)
        self.trust.setCurrentText(config.trust)
        self.allow_tools.setText(", ".join(config.allow_tools))
        self.deny_tools.setText(", ".join(config.deny_tools))
        self.capability_tags.setText(", ".join(config.capability_tags))
        self.auth.setCurrentText(config.auth_type)
        self.username.setText(config.auth_username)
        self.auth_header.setText(config.auth_header)
        self.oauth_client_id.setText(config.oauth_client_id)
        self.oauth_authorization_url.setText(config.oauth_authorization_url)
        self.oauth_token_url.setText(config.oauth_token_url)
        self.oauth_scopes.setText(", ".join(config.oauth_scopes))
        # Deliberately never call get_mcp_secret here.
        status = authentication_status(name)
        auth_text = "configured" if status.get("configured") else "not configured"
        self.status.setText(f"Loaded {name} · authentication {auth_text} · secret fields are intentionally blank")
        self._transport_changed(config.transport)
        self._auth_changed(config.auth_type)

    def _config(self) -> MCPServerConfig:
        try:
            args = shlex.split(self.arguments.text()) if self.arguments.text().strip() else []
        except ValueError as exc:
            raise ValueError(f"invalid stdio arguments: {exc}") from exc
        transport = self.transport.currentText()
        return MCPServerConfig(
            name=self.name.text().strip(), enabled=self.enabled.isChecked(),
            transport=transport, url=self.url.text().strip() if transport == "streamable-http" else "",
            command=self.command.text().strip() if transport == "stdio" else "",
            args=args if transport == "stdio" else [],
            env_names=self._csv(self.env_names.text()) if transport == "stdio" else [],
            timeout=self.timeout.value(), trust=self.trust.currentText(),
            allow_tools=self._csv(self.allow_tools.text()), deny_tools=self._csv(self.deny_tools.text()),
            capability_tags=self._csv(self.capability_tags.text()),
            auth_type=self.auth.currentText() if transport == "streamable-http" else "none",
            auth_username=self.username.text().strip(), auth_header=self.auth_header.text().strip() or "X-API-Key",
            oauth_client_id=self.oauth_client_id.text().strip(),
            oauth_authorization_url=self.oauth_authorization_url.text().strip(),
            oauth_token_url=self.oauth_token_url.text().strip(),
            oauth_scopes=self._csv(self.oauth_scopes.text()),
        )

    def _secrets(self, config: MCPServerConfig) -> dict[str, str]:
        values: dict[str, str] = {}
        field = required_secret_field(config)
        if field and self.secret.text():
            values[field] = self.secret.text()
        if config.auth_type == "oauth2" and self.oauth_client_secret.text():
            values["oauth_client_secret"] = self.oauth_client_secret.text()
        return values

    def save(self) -> None:
        try:
            config = self._config()
            secrets = self._secrets(config)
            existing = get_server(config.name)
            oauth_ready = bool(existing and authentication_status(config.name).get("configured"))
            if config.auth_type == "oauth2" and not oauth_ready:
                check = authorize_and_save_server(config, secrets=secrets)
            else:
                check = save_server(config, secrets=secrets, test=True)
            self.secret.clear()
            self.oauth_client_secret.clear()
            self._selected_name = config.name
            self._connection_state[config.name] = bool(check.get("ok"))
            self.status.setText(f"Saved and connected · {check.get('tool_count', 0)} tools")
            self.refresh()
        except Exception as exc:
            QMessageBox.critical(self, "MCP server", f"{type(exc).__name__}: {exc}")

    def authorize(self) -> None:
        try:
            config = self._config()
            secrets = self._secrets(config)
            result = authorize_and_save_server(config, secrets=secrets)
            self.secret.clear()
            self.oauth_client_secret.clear()
            self._connection_state[config.name] = True
            self.status.setText("OAuth authorized and MCP connection verified")
            self.refresh()
        except Exception as exc:
            QMessageBox.critical(self, "OAuth", f"{type(exc).__name__}: {exc}")

    def _triforce_tools(self) -> list[dict]:
        """Use the existing authenticated TriForce client/RBAC path on explicit user action."""
        from ..cli import session_client
        from ..executor import AGENT_TOOLS
        from ..tool_policy import filter_tool_catalog
        _, client = session_client()
        payload = {"jsonrpc": "2.0", "method": "tools/list", "params": {}, "id": 1}
        data = client._request("POST", "/v1/mcp", payload, require_auth=True, _label="tools/list")
        result = data.get("result") if isinstance(data, dict) else {}
        tools = result.get("tools", []) if isinstance(result, dict) else []
        return filter_tool_catalog(tools, AGENT_TOOLS)

    def test(self) -> None:
        name = self._selected_name or self.name.text().strip()
        if name == "triforce":
            try:
                tools = self._triforce_tools()
                self._connection_state[name] = True
                self.status.setText(f"TriForce connected via built-in login/RBAC path · {len(tools)} tools")
            except Exception as exc:
                self._connection_state[name] = False
                self.status.setText(f"TriForce connection failed · {type(exc).__name__}: {exc}")
            self.refresh()
            return
        try:
            config = self._config()
            check = test_candidate(config, secrets=self._secrets(config))
            self._connection_state[name] = bool(check.get("ok"))
            self.status.setText(("Connected" if check.get("ok") else "Failed") + f" · {check.get('tool_count', 0)} tools · {check.get('error', '')}")
            self.refresh()
        except Exception as exc:
            self._connection_state[name] = False
            self.status.setText(f"{type(exc).__name__}: {exc}")
            self.refresh()

    def remove(self) -> None:
        name = self._selected_name or self.name.text().strip()
        if not name or name == "triforce":
            return
        try:
            if QMessageBox.question(self, "Remove MCP server", f"Remove {name} and its stored MCP credentials?") != QMessageBox.StandardButton.Yes:
                return
            if remove_server(name):
                self._connection_state.pop(name, None)
                self.clear()
                self.refresh()
        except Exception as exc:
            QMessageBox.critical(self, "MCP server", f"{type(exc).__name__}: {exc}")

    def toggle_enabled(self) -> None:
        name = self._selected_name or self.name.text().strip()
        if not name or name == "triforce":
            return
        try:
            current = get_server(name)
            if current is None:
                raise ValueError(f"unknown MCP server: {name}")
            updated = set_server_enabled(name, not current.enabled)
            self.enabled.setChecked(updated.enabled)
            self.status.setText(f"{name} → {'enabled' if updated.enabled else 'disabled'}")
            self.refresh()
        except Exception as exc:
            QMessageBox.critical(self, "MCP server", f"{type(exc).__name__}: {exc}")

    def tools(self) -> None:
        name = self._selected_name or self.name.text().strip()
        try:
            rows = self._triforce_tools() if name == "triforce" else server_tools(name)
            dialog = QDialog(self)
            dialog.setWindowTitle(f"MCP Tools · {name}")
            dialog.resize(820, 420)
            layout = QVBoxLayout(dialog)
            table = QTableWidget(len(rows), 4)
            table.setHorizontalHeaderLabels(["Tool Name", "Description", "Enabled / Allowed", "Read-only Hint"])
            for row_index, tool in enumerate(rows):
                annotations = tool.get("annotations") if isinstance(tool.get("annotations"), dict) else {}
                values = [
                    str(tool.get("name") or ""), str(tool.get("description") or ""), "yes",
                    "yes" if annotations.get("readOnlyHint") is True else "no / approval required",
                ]
                for column, value in enumerate(values):
                    table.setItem(row_index, column, QTableWidgetItem(value))
            table.resizeColumnsToContents()
            layout.addWidget(table)
            close = QPushButton("Close")
            close.clicked.connect(dialog.accept)
            layout.addWidget(close)
            dialog.exec()
        except Exception as exc:
            QMessageBox.critical(self, "MCP tools", f"{type(exc).__name__}: {exc}")
