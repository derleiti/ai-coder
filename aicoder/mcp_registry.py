"""First-class external MCP server registry and minimal MCP client transports."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading
from typing import Any
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlsplit
from urllib.request import Request, urlopen

from .config import CONFIG_DIR, atomic_write_private

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_SECRET_KEY_RE = re.compile(r"token|secret|password|passwd|api[_-]?key|authorization", re.I)
_SAFE_ENV = ("PATH", "HOME", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "XDG_RUNTIME_DIR")
_TRANSPORTS = {"stdio", "streamable-http"}
_AUTH_TYPES = {"none", "api-key", "bearer", "basic", "oauth2", "custom-header"}
_FORBIDDEN_AUTH_HEADERS = {
    "authorization", "proxy-authorization", "proxy-authenticate", "host",
    "content-length", "connection", "transfer-encoding", "upgrade",
    "cookie", "set-cookie", "mcp-session-id",
}
_PREFIX = "mcp."


class MCPRegistryError(ValueError):
    pass


def parse_header_env(items: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in items:
        if "=" not in str(item):
            raise MCPRegistryError("--header-env requires HEADER=ENV_NAME")
        header, env_name = str(item).split("=", 1)
        header, env_name = header.strip(), env_name.strip()
        if not header or not env_name:
            raise MCPRegistryError("--header-env requires HEADER=ENV_NAME")
        result[header] = env_name
    return result


@dataclass
class MCPServerConfig:
    name: str
    enabled: bool = True
    transport: str = "stdio"
    command: str = ""
    url: str = ""
    args: list[str] = field(default_factory=list)
    env_names: list[str] = field(default_factory=list)
    allow_tools: list[str] = field(default_factory=list)
    deny_tools: list[str] = field(default_factory=list)
    trust: str = "untrusted"
    timeout: int = 30
    capability_tags: list[str] = field(default_factory=list)
    header_env: dict[str, str] = field(default_factory=dict)
    auth_type: str = "none"
    auth_username: str = ""
    auth_header: str = "X-API-Key"
    oauth_authorization_url: str = ""
    oauth_token_url: str = ""
    oauth_client_id: str = ""
    oauth_scopes: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MCPServerConfig":
        return cls(
            name=str(data.get("name") or ""), enabled=bool(data.get("enabled", True)),
            transport=str(data.get("transport") or "stdio"), command=str(data.get("command") or ""),
            url=str(data.get("url") or ""), args=[str(x) for x in data.get("args") or []],
            env_names=[str(x) for x in data.get("env_names") or []],
            allow_tools=[str(x) for x in data.get("allow_tools") or []],
            deny_tools=[str(x) for x in data.get("deny_tools") or []],
            trust=str(data.get("trust") or "untrusted"), timeout=int(data.get("timeout") or 30),
            capability_tags=[str(x) for x in data.get("capability_tags") or []],
            header_env={str(k): str(v) for k, v in (data.get("header_env") or {}).items()} if isinstance(data.get("header_env"), dict) else {},
            auth_type=str(data.get("auth_type") or "none"), auth_username=str(data.get("auth_username") or ""),
            auth_header=str(data.get("auth_header") or "X-API-Key"),
            oauth_authorization_url=str(data.get("oauth_authorization_url") or ""), oauth_token_url=str(data.get("oauth_token_url") or ""),
            oauth_client_id=str(data.get("oauth_client_id") or ""), oauth_scopes=[str(x) for x in data.get("oauth_scopes") or []],
        )


def _validate(config: MCPServerConfig) -> MCPServerConfig:
    if not _NAME_RE.fullmatch(config.name):
        raise MCPRegistryError("invalid MCP server name")
    if config.name.lower() == "triforce":
        raise MCPRegistryError("'triforce' is reserved for the built-in server profile")
    if config.auth_type not in _AUTH_TYPES:
        raise MCPRegistryError(f"unsupported MCP authentication: {config.auth_type}")
    if config.auth_type in {"api-key", "custom-header"}:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,63}", config.auth_header):
            raise MCPRegistryError("invalid authentication header")
        if config.auth_header.lower() in _FORBIDDEN_AUTH_HEADERS:
            raise MCPRegistryError(f"reserved authentication header: {config.auth_header}")
    if config.auth_type == "basic" and not config.auth_username.strip():
        raise MCPRegistryError("basic authentication requires a username")
    if config.auth_type == "oauth2":
        if config.transport != "streamable-http":
            raise MCPRegistryError("OAuth is supported only for streamable-http MCP servers")
        if not config.oauth_client_id.strip():
            raise MCPRegistryError("OAuth requires a registered client id")
    if config.transport not in _TRANSPORTS:
        raise MCPRegistryError(f"unsupported MCP transport: {config.transport}")
    if not 1 <= int(config.timeout) <= 300:
        raise MCPRegistryError("timeout must be between 1 and 300 seconds")
    if config.trust not in {"untrusted", "trusted"}:
        raise MCPRegistryError("trust must be 'untrusted' or 'trusted'")
    if config.transport == "stdio":
        if not config.command.strip():
            raise MCPRegistryError("stdio MCP server requires --command")
        if config.url:
            raise MCPRegistryError("stdio MCP server cannot also define a URL")
    else:
        if not config.url.strip():
            raise MCPRegistryError("streamable-http MCP server requires --url")
        parsed = urlsplit(config.url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise MCPRegistryError("MCP URL must be http(s) with a host")
        if parsed.username or parsed.password:
            raise MCPRegistryError("credentials must not be embedded in MCP URLs")
        for key, _value in parse_qsl(parsed.query, keep_blank_values=True):
            if _SECRET_KEY_RE.search(key):
                raise MCPRegistryError("secret-bearing query parameters are forbidden in MCP URLs")
        if config.command:
            raise MCPRegistryError("streamable-http MCP server cannot also define a command")
    for name in config.env_names:
        if not _ENV_RE.fullmatch(name):
            raise MCPRegistryError(f"invalid environment variable name: {name}")
    if config.transport == "streamable-http" and config.header_env:
        raise MCPRegistryError("HTTP header environment injection is disabled; use keyring-backed authentication")
    forbidden_headers = _FORBIDDEN_AUTH_HEADERS
    for header, env_name in config.header_env.items():
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,63}", header) or header.lower() in forbidden_headers:
            raise MCPRegistryError(f"invalid or reserved HTTP header: {header}")
        if not _ENV_RE.fullmatch(env_name):
            raise MCPRegistryError(f"invalid environment variable name: {env_name}")
    config.args = list(config.args)
    config.env_names = list(dict.fromkeys(config.env_names))
    config.allow_tools = list(dict.fromkeys(config.allow_tools))
    config.deny_tools = list(dict.fromkeys(config.deny_tools))
    config.capability_tags = list(dict.fromkeys(config.capability_tags))
    return config


class MCPRegistry:
    def __init__(self, path: Path | None = None):
        self.path = path or (CONFIG_DIR / "mcp_servers.json")

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, TypeError) as exc:
            raise MCPRegistryError(f"invalid MCP registry: {exc}") from exc

    def _write(self, servers: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_private(self.path, json.dumps({"schema": 1, "servers": servers}, indent=2, sort_keys=True) + "\n")

    def list(self, *, include_builtin: bool = True) -> list[dict[str, Any]]:
        data = self._read().get("servers", {})
        rows: list[dict[str, Any]] = []
        if include_builtin:
            rows.append({"name": "triforce", "enabled": True, "transport": "builtin", "trust": "builtin", "builtin": True})
        if isinstance(data, dict):
            for name in sorted(data):
                raw = data[name]
                if isinstance(raw, dict):
                    row = asdict(MCPServerConfig.from_dict(raw)); row["builtin"] = False; rows.append(row)
        return rows

    def get(self, name: str) -> MCPServerConfig | None:
        if name == "triforce":
            return None
        data = self._read().get("servers", {})
        raw = data.get(name) if isinstance(data, dict) else None
        return MCPServerConfig.from_dict(raw) if isinstance(raw, dict) else None

    def put(self, config: MCPServerConfig) -> MCPServerConfig:
        config = _validate(config)
        data = self._read(); servers = data.get("servers", {})
        if not isinstance(servers, dict): servers = {}
        servers[config.name] = asdict(config)
        self._write(servers)
        return config

    def remove(self, name: str) -> bool:
        if name == "triforce": raise MCPRegistryError("built-in TriForce profile cannot be removed")
        data = self._read(); servers = data.get("servers", {})
        if not isinstance(servers, dict) or name not in servers: return False
        del servers[name]; self._write(servers); return True

    def set_enabled(self, name: str, enabled: bool) -> MCPServerConfig:
        config = self.get(name)
        if config is None: raise MCPRegistryError(f"unknown MCP server: {name}")
        config.enabled = bool(enabled); return self.put(config)


def _sanitized_env(config: MCPServerConfig) -> dict[str, str]:
    env: dict[str, str] = {}
    for name in _SAFE_ENV:
        value = os.environ.get(name)
        if value is not None: env[name] = value
    for name in config.env_names:
        value = os.environ.get(name)
        if value is not None: env[name] = value
    return env


def _readline_timeout(stream, timeout: int) -> str:
    result: queue.Queue[str] = queue.Queue(maxsize=1)
    threading.Thread(target=lambda: result.put(stream.readline()), daemon=True).start()
    try:
        line = result.get(timeout=timeout)
    except queue.Empty as exc:
        raise TimeoutError("MCP stdio response timed out") from exc
    if not line:
        raise RuntimeError("MCP stdio server closed its output")
    return line


def _json_response(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError("invalid MCP JSON-RPC response")
    if value.get("error") is not None:
        error = value.get("error")
        code = error.get("code") if isinstance(error, dict) else "unknown"
        raise RuntimeError(f"MCP JSON-RPC request failed (code {code})")
    return value


class _StdioSession:
    def __init__(self, config: MCPServerConfig): self.config=config; self.proc=None; self.next_id=1
    def __enter__(self):
        command = shutil.which(self.config.command) or self.config.command
        self.proc=subprocess.Popen([command,*self.config.args],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,bufsize=1,env=_sanitized_env(self.config))
        self.request("initialize", {"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"aicoder","version":"1.2"}})
        self.notify("notifications/initialized", {})
        return self
    def __exit__(self,*_):
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill(); self.proc.wait(timeout=2)
            for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                try:
                    if stream is not None: stream.close()
                except OSError:
                    pass
    def notify(self,method:str,params:dict[str,Any]):
        assert self.proc and self.proc.stdin
        self.proc.stdin.write(json.dumps({"jsonrpc":"2.0","method":method,"params":params})+"\n"); self.proc.stdin.flush()
    def request(self,method:str,params:dict[str,Any]) -> dict[str,Any]:
        assert self.proc and self.proc.stdin and self.proc.stdout
        ident=self.next_id; self.next_id+=1
        self.proc.stdin.write(json.dumps({"jsonrpc":"2.0","id":ident,"method":method,"params":params})+"\n"); self.proc.stdin.flush()
        while True:
            msg=_json_response(json.loads(_readline_timeout(self.proc.stdout,self.config.timeout)))
            if msg.get("id")==ident: return msg


class _HttpSession:
    def __init__(self, config: MCPServerConfig):
        self.config = config
        self.session_id = ""
        self.protocol_version = ""
        self.next_id = 1

    def __enter__(self):
        response = self.request("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "aicoder", "version": "1.2"},
        })
        result = response.get("result") if isinstance(response.get("result"), dict) else {}
        self.protocol_version = str(result.get("protocolVersion") or "2025-06-18")
        self.notify("notifications/initialized", {})
        return self

    def __exit__(self, *_):
        if not self.session_id:
            return None
        try:
            headers = self._headers()
            request = Request(self.config.url, headers=headers, method="DELETE")
            with urlopen(request, timeout=min(5, self.config.timeout)):
                pass
        except Exception:
            # Session termination is best-effort by protocol design.
            pass
        return None

    @staticmethod
    def _parse(body: str, content_type: str, expected_id: Any = None) -> dict[str, Any]:
        if "text/event-stream" in content_type:
            data_lines: list[str] = []
            first_message: dict[str, Any] | None = None

            def consume() -> dict[str, Any] | None:
                nonlocal data_lines, first_message
                if not data_lines:
                    return None
                value = json.loads("\n".join(data_lines))
                data_lines = []
                if not isinstance(value, dict):
                    return None
                if first_message is None:
                    first_message = value
                if expected_id is None or value.get("id") == expected_id:
                    return value
                return None

            for raw_line in body.splitlines():
                line = raw_line.rstrip("\r")
                if not line:
                    matched = consume()
                    if matched is not None:
                        return matched
                    continue
                if line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
            matched = consume()
            if matched is not None:
                return matched
            return first_message or {}
        if not body.strip():
            return {}
        value = json.loads(body)
        return value if isinstance(value, dict) else {}

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        from .mcp_credentials import auth_headers
        headers.update(auth_headers(self.config))
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        return headers

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = self._headers()
        request = Request(self.config.url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        with urlopen(request, timeout=self.config.timeout) as response:
            if response.headers.get("Mcp-Session-Id"):
                self.session_id = response.headers["Mcp-Session-Id"]
            body = response.read().decode("utf-8", errors="replace")
            return self._parse(body, response.headers.get("Content-Type", ""), payload.get("id"))

    def notify(self, method: str, params: dict[str, Any]):
        self._post({"jsonrpc": "2.0", "method": method, "params": params})

    def _restart_session(self) -> None:
        self.session_id = ""
        self.protocol_version = ""
        response = self.request("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "aicoder", "version": "1.2"},
        })
        result = response.get("result") if isinstance(response.get("result"), dict) else {}
        self.protocol_version = str(result.get("protocolVersion") or "2025-06-18")
        self.notify("notifications/initialized", {})

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        ident = self.next_id
        self.next_id += 1
        payload = {"jsonrpc": "2.0", "id": ident, "method": method, "params": params}
        try:
            raw = self._post(payload)
        except HTTPError as exc:
            if exc.code == 404 and self.session_id and method != "initialize":
                self._restart_session()
                raw = self._post(payload)
            elif exc.code == 401 and self.config.auth_type == "oauth2":
                from .mcp_oauth import refresh_oauth_token
                refresh_oauth_token(self.config)
                raw = self._post(payload)
            else:
                raise
        response = _json_response(raw)
        if response.get("id") != ident:
            raise RuntimeError("MCP JSON-RPC response id mismatch")
        return response


def _session(config:MCPServerConfig):
    return _StdioSession(config) if config.transport=="stdio" else _HttpSession(config)


def _allowed(config:MCPServerConfig,name:str) -> bool:
    if config.allow_tools and name not in set(config.allow_tools): return False
    if name in set(config.deny_tools): return False
    return True


def list_server_tools(config:MCPServerConfig) -> list[dict[str,Any]]:
    _validate(config)
    with _session(config) as session:
        result=session.request("tools/list",{}).get("result",{})
    tools=result.get("tools",[]) if isinstance(result,dict) else []
    return [dict(tool) for tool in tools if isinstance(tool,dict) and tool.get("name") and _allowed(config,str(tool["name"]))]


def doctor_server(config:MCPServerConfig) -> dict[str,Any]:
    try:
        tools=list_server_tools(config)
        return {"name":config.name,"ok":True,"transport":config.transport,"tool_count":len(tools),"env_names":list(config.env_names),"error":""}
    except Exception as exc:
        return {"name":config.name,"ok":False,"transport":config.transport,"tool_count":0,"env_names":list(config.env_names),"error":f"{type(exc).__name__}: {exc}"}


def namespaced_tool_name(server:str,tool:str) -> str: return f"{_PREFIX}{server}.{tool}"

def split_namespaced_tool(name:str) -> tuple[str,str] | None:
    if not name.startswith(_PREFIX): return None
    rest=name[len(_PREFIX):]
    server,sep,tool=rest.partition(".")
    return (server,tool) if sep and server and tool else None


def external_tool_schemas(registry:MCPRegistry|None=None) -> list[dict[str,Any]]:
    registry=registry or MCPRegistry(); out=[]
    for row in registry.list(include_builtin=False):
        config=MCPServerConfig.from_dict(row)
        if not config.enabled: continue
        try: tools=list_server_tools(config)
        except Exception: continue
        for tool in tools:
            original=str(tool.get("name") or "")
            schema=dict(tool); schema["name"]=namespaced_tool_name(config.name,original)
            schema["description"]=f"[{config.name}] {str(tool.get('description') or original)}"
            caps=[str(x) for x in tool.get("capabilities") or [] if str(x)] + config.capability_tags
            if caps: schema["capabilities"]=list(dict.fromkeys(caps))
            annotations=dict(tool.get("annotations") or {}) if isinstance(tool.get("annotations"),dict) else {}
            # An untrusted server cannot self-declare its way around local approval.
            # Trusted servers may supply MCP safety hints; unknown hints still fail closed.
            if config.trust != "trusted":
                annotations["readOnlyHint"] = False
            elif "readOnlyHint" not in annotations:
                annotations["readOnlyHint"] = False
            schema["annotations"]=annotations
            out.append(schema)
    return out


def call_external_tool(name:str,args:dict[str,Any],registry:MCPRegistry|None=None) -> tuple[str,bool]:
    parts=split_namespaced_tool(name)
    if parts is None: return f"invalid external MCP tool name: {name}",True
    server,tool=parts; registry=registry or MCPRegistry(); config=registry.get(server)
    if config is None or not config.enabled: return f"external MCP server unavailable: {server}",True
    if not _allowed(config,tool): return f"external MCP tool blocked by server filter: {tool}",True
    try:
        with _session(config) as session:
            response=session.request("tools/call",{"name":tool,"arguments":dict(args)})
        result=response.get("result",{})
        if not isinstance(result,dict): return str(result),False
        texts=[]
        for block in result.get("content",[]) if isinstance(result.get("content"),list) else []:
            if isinstance(block,dict) and isinstance(block.get("text"),str): texts.append(block["text"])
        if not texts and result.get("structuredContent") is not None: texts.append(json.dumps(result["structuredContent"],ensure_ascii=False,indent=2))
        return "\n".join(texts)[:12000],bool(result.get("isError"))
    except Exception as exc:
        return f"external MCP call failed: {type(exc).__name__}: {exc}",True
