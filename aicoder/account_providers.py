"""Official account-backed model integrations for AICoder.

This module intentionally does *not* extract OAuth/access tokens from provider
clients.  Each provider keeps ownership of its credentials:

* ChatGPT: Codex App Server managed ChatGPT OAuth (`account/login/start`).
* Claude: Claude Code's `claude auth` commands and non-interactive print mode.
* Mistral: Vibe's setup/login and programmatic mode.
* Gemini: Gemini CLI's Google login and headless mode.

AICoder stores only provider IDs in ``linked_account_providers``.  Account model
IDs use ``account:<provider>/<model>``.  Once such a model is selected routing is
fail-closed: it can never fall through to TriForce or a BYOK API transport.
"""
from __future__ import annotations

import json
import os
import queue
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .client import ClientError
from .session_state import get_state, set_linked_account_providers

ACCOUNT_PREFIX = "account:"


@dataclass(frozen=True)
class AccountProviderSpec:
    id: str
    display_name: str
    executable: str
    models: tuple[tuple[str, str], ...] = ()
    auth_verifiable: bool = False
    dynamic_models: bool = False


ACCOUNT_PROVIDERS: tuple[AccountProviderSpec, ...] = (
    AccountProviderSpec("chatgpt", "ChatGPT / OpenAI", "codex", auth_verifiable=True, dynamic_models=True),
    AccountProviderSpec(
        "claude", "Claude / Anthropic", "claude", auth_verifiable=True,
        models=(("sonnet", "Claude Sonnet"), ("opus", "Claude Opus"), ("haiku", "Claude Haiku"), ("fable", "Claude Fable")),
    ),
    AccountProviderSpec(
        "mistral", "Mistral", "vibe",
        models=(
            ("mistral-medium-latest", "Mistral Medium"),
            ("zai-glm-5-2", "Z.ai GLM 5.2"),
            ("mistral-large-latest", "Mistral Large"),
            ("mistral-small-latest", "Mistral Small"),
            ("codestral-latest", "Codestral"),
            ("ministral-14b-latest", "Ministral 14B"),
            ("ministral-8b-latest", "Ministral 8B"),
            ("ministral-3b-latest", "Ministral 3B"),
        ),
    ),
    AccountProviderSpec(
        "gemini", "Google Gemini", "gemini",
        models=(("auto", "Gemini Auto"), ("pro", "Gemini Pro"), ("flash", "Gemini Flash"), ("flash-lite", "Gemini Flash-Lite")),
    ),
)

_PROVIDER_MAP = {item.id: item for item in ACCOUNT_PROVIDERS}


def provider_spec(provider: str) -> AccountProviderSpec:
    key = str(provider or "").strip().lower()
    try:
        return _PROVIDER_MAP[key]
    except KeyError as exc:
        raise ClientError(f"Unsupported account provider: {provider!r}") from exc


def is_account_model(model: str | None) -> bool:
    return str(model or "").strip().startswith(ACCOUNT_PREFIX)


def account_model_id(provider: str, model: str) -> str:
    spec = provider_spec(provider)
    value = str(model or "").strip()
    if not value or "/" in spec.id:
        raise ClientError("Account model id is empty or invalid")
    return f"{ACCOUNT_PREFIX}{spec.id}/{value}"


def parse_account_model(model: str | None) -> tuple[str, str]:
    raw = str(model or "").strip()
    if not raw.startswith(ACCOUNT_PREFIX):
        raise ClientError(f"Not an account model id: {raw or '<empty>'}")
    remainder = raw[len(ACCOUNT_PREFIX):]
    if "/" not in remainder:
        raise ClientError(f"Malformed account model id: {raw}")
    provider, provider_model = remainder.split("/", 1)
    provider_spec(provider)
    if not provider_model.strip():
        raise ClientError(f"Malformed account model id: {raw}")
    return provider.lower(), provider_model.strip()


def linked_provider_ids() -> list[str]:
    raw = get_state().get("linked_account_providers") or []
    if not isinstance(raw, list):
        return []
    known = set(_PROVIDER_MAP)
    return sorted({str(item).strip().lower() for item in raw if str(item).strip().lower() in known})


def set_provider_linked(provider: str, linked: bool) -> None:
    key = provider_spec(provider).id
    current = set(linked_provider_ids())
    if linked:
        current.add(key)
    else:
        current.discard(key)
    set_linked_account_providers(sorted(current))


def _which(spec: AccountProviderSpec) -> str:
    return _which_executable(spec.executable)


_INSTALL_RECIPES: dict[str, tuple[str, ...]] = {
    "chatgpt": ("npm", "install", "-g", "@openai/codex@latest"),
    "claude": ("npm", "install", "-g", "@anthropic-ai/claude-code@latest"),
    "gemini": ("npm", "install", "-g", "@google/gemini-cli@latest"),
    "mistral": ("uv", "tool", "install", "--upgrade", "mistral-vibe"),
}


def _augmented_path() -> str:
    """Return PATH including common user-local install locations."""
    home = Path.home()
    parts = [
        str(home / ".npm-global" / "bin"),
        str(home / ".local" / "bin"),
        str(home / ".cargo" / "bin"),
        os.environ.get("PATH", ""),
    ]
    return os.pathsep.join(part for part in parts if part)


def _which_executable(name: str) -> str:
    return str(shutil.which(name, path=_augmented_path()) or "")


def ensure_provider_client(provider: str) -> str:
    """Install a missing official provider CLI into the user's normal tool path.

    No sudo/system package mutation is attempted.  npm uses the user's configured
    global prefix; Mistral uses ``uv tool install``.
    """
    spec = provider_spec(provider)
    existing = _which_executable(spec.executable)
    if existing:
        return existing
    recipe = _INSTALL_RECIPES.get(spec.id)
    if not recipe:
        raise ClientError(f"No supported installer is configured for {spec.display_name}")
    runner = _which_executable(recipe[0])
    if not runner:
        dependency = "Node.js/npm" if recipe[0] == "npm" else "uv"
        raise ClientError(f"{dependency} is required to install the official {spec.display_name} client")
    if recipe[0] == "npm":
        argv = [runner, "install", "-g", "--prefix", str(Path.home() / ".local"), recipe[-1]]
    else:
        argv = [runner, *recipe[1:]]
    env = dict(os.environ)
    env["PATH"] = _augmented_path()
    try:
        proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=300, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClientError(f"Could not install the official {spec.display_name} client") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:]
        suffix = f": {detail[0][:240]}" if detail else ""
        raise ClientError(f"Official {spec.display_name} client installation failed{suffix}")
    installed = _which_executable(spec.executable)
    if not installed:
        raise ClientError(f"{spec.display_name} client installed but executable '{spec.executable}' is still not on PATH")
    return installed


class CodexAppServer:
    """Small stable-surface JSONL client for ``codex app-server``.

    No experimental capabilities are enabled.  Authentication remains fully
    managed by Codex, so AICoder never sees ChatGPT access or refresh tokens.
    """

    def __init__(self, *, timeout: int = 30):
        executable = _which_executable("codex")
        if not executable:
            raise ClientError(
                "Codex CLI is not installed. Install the official Codex CLI first, then link ChatGPT again."
            )
        self.timeout = max(5, int(timeout))
        try:
            self.proc = subprocess.Popen(
                [executable, "app-server"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", bufsize=1,
            )
        except OSError as exc:
            raise ClientError("Could not start the official Codex App Server") from exc
        if self.proc.stdin is None or self.proc.stdout is None:
            self.close()
            raise ClientError("Codex App Server stdio transport is unavailable")
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        self._next_id = 1
        self._initialize()

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            try:
                message = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(message, dict):
                self._queue.put(message)

    def _send(self, payload: dict[str, Any]) -> None:
        if self.proc.poll() is not None:
            raise ClientError(f"Codex App Server exited unexpectedly (code {self.proc.returncode})")
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise ClientError("Codex App Server connection closed") from exc

    def _receive(self, *, timeout: float | None = None) -> dict[str, Any]:
        wait = self.timeout if timeout is None else max(0.1, float(timeout))
        try:
            return self._queue.get(timeout=wait)
        except queue.Empty as exc:
            raise ClientError("Codex App Server timed out") from exc

    def _request(self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        payload: dict[str, Any] = {"method": method, "id": request_id}
        if params is not None:
            payload["params"] = params
        self._send(payload)
        deadline = time.monotonic() + (self.timeout if timeout is None else float(timeout))
        deferred: list[dict[str, Any]] = []
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ClientError(f"Codex App Server request timed out: {method}")
                message = self._receive(timeout=remaining)
                if message.get("id") == request_id:
                    if message.get("error") is not None:
                        error = message.get("error") if isinstance(message.get("error"), dict) else {}
                        text = str(error.get("message") or "request failed")
                        raise ClientError(f"Codex App Server {method} failed: {text[:500]}")
                    result = message.get("result")
                    return result if isinstance(result, dict) else {}
                deferred.append(message)
        finally:
            for item in deferred:
                self._queue.put(item)

    def _initialize(self) -> None:
        self._request("initialize", {
            "clientInfo": {
                "name": "ailinux_aicoder",
                "title": "AILinux AICoder",
                "version": __version__,
            }
        })
        self._send({"method": "initialized", "params": {}})

    def wait_notification(self, method: str, *, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + float(timeout)
        deferred: list[dict[str, Any]] = []
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ClientError(f"Timed out waiting for Codex event {method}")
                message = self._receive(timeout=remaining)
                if message.get("method") == method and "id" not in message:
                    params = message.get("params")
                    return params if isinstance(params, dict) else {}
                deferred.append(message)
        finally:
            for item in deferred:
                self._queue.put(item)

    def account_read(self) -> dict[str, Any]:
        return self._request("account/read", {"refreshToken": False})

    def model_list(self) -> list[dict[str, Any]]:
        models: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(20):
            params: dict[str, Any] = {"limit": 100, "includeHidden": False}
            if cursor:
                params["cursor"] = cursor
            result = self._request("model/list", params)
            data = result.get("data") or []
            if isinstance(data, list):
                models.extend(dict(item) for item in data if isinstance(item, dict))
            cursor = str(result.get("nextCursor") or "").strip() or None
            if not cursor:
                break
        return models

    def login_chatgpt(self, *, timeout: int = 300, open_browser: bool = True) -> dict[str, Any]:
        result = self._request("account/login/start", {
            "type": "chatgpt",
            "useHostedLoginSuccessPage": True,
            "appBrand": "chatgpt",
        })
        login_id = str(result.get("loginId") or "")
        auth_url = str(result.get("authUrl") or "")
        if not login_id or not auth_url:
            raise ClientError("Codex did not return a ChatGPT login URL")
        if open_browser:
            try:
                webbrowser.open(auth_url)
            except Exception:
                pass
        deadline = time.monotonic() + max(30, int(timeout))
        deferred: list[dict[str, Any]] = []
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ClientError(f"ChatGPT login timed out. Open this URL manually: {auth_url}")
                message = self._receive(timeout=remaining)
                if message.get("method") == "account/login/completed":
                    params = message.get("params") if isinstance(message.get("params"), dict) else {}
                    if str(params.get("loginId") or "") != login_id:
                        deferred.append(message)
                        continue
                    if not params.get("success"):
                        raise ClientError("ChatGPT login was not completed successfully")
                    account = self.account_read()
                    account["authUrl"] = auth_url
                    return account
                deferred.append(message)
        finally:
            for item in deferred:
                self._queue.put(item)

    def logout(self) -> None:
        self._request("account/logout")

    def close(self) -> None:
        proc = getattr(self, "proc", None)
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def __enter__(self) -> "CodexAppServer":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _chatgpt_status() -> dict[str, Any]:
    spec = provider_spec("chatgpt")
    installed = bool(_which(spec))
    if not installed:
        return {"provider": spec.id, "display": spec.display_name, "installed": False, "linked": False,
                "authenticated": False, "detail": "Codex CLI fehlt"}
    try:
        with CodexAppServer(timeout=8) as server:
            result = server.account_read()
    except Exception as exc:
        return {"provider": spec.id, "display": spec.display_name, "installed": True,
                "linked": False, "authenticated": False, "detail": f"Codex nicht verbunden: {str(exc)[:100]}"}
    account = result.get("account") if isinstance(result.get("account"), dict) else {}
    authenticated = account.get("type") == "chatgpt"
    detail = "ChatGPT nicht angemeldet"
    plan = str(account.get("planType") or "").strip()
    email = str(account.get("email") or "").strip()
    if authenticated:
        detail = "Verbunden" + (f" · {plan}" if plan else "") + (f" · {email}" if email else "")
    return {"provider": spec.id, "display": spec.display_name, "installed": True,
            "linked": authenticated, "authenticated": authenticated, "detail": detail, "plan": plan, "email": email}


def _claude_status() -> dict[str, Any]:
    spec = provider_spec("claude")
    executable = _which(spec)
    marked = spec.id in linked_provider_ids()
    if not executable:
        return {"provider": spec.id, "display": spec.display_name, "installed": False,
                "linked": marked, "authenticated": False, "detail": "Claude Code fehlt"}
    try:
        proc = subprocess.run([executable, "auth", "status"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
        authenticated = proc.returncode == 0
    except Exception:
        authenticated = False
    return {"provider": spec.id, "display": spec.display_name, "installed": True,
            "linked": bool(marked or authenticated), "authenticated": authenticated,
            "detail": "Verbunden" if authenticated else ("Verknüpft · Login noch erforderlich" if marked else "Nicht verbunden")}


def account_status(provider: str) -> dict[str, Any]:
    spec = provider_spec(provider)
    if spec.id == "chatgpt":
        return _chatgpt_status()
    if spec.id == "claude":
        return _claude_status()
    installed = bool(_which(spec))
    marked = spec.id in linked_provider_ids()
    authenticated: bool | None = None
    detail = "Verknüpft · Login vom offiziellen Client verwaltet" if marked and installed else (
        f"{spec.display_name}-CLI fehlt" if not installed else "Nicht verbunden"
    )
    return {"provider": spec.id, "display": spec.display_name, "installed": installed,
            "linked": bool(marked), "authenticated": authenticated, "detail": detail}


def account_statuses() -> list[dict[str, Any]]:
    return [account_status(spec.id) for spec in ACCOUNT_PROVIDERS]


def available_account_models(provider: str) -> list[dict[str, Any]]:
    spec = provider_spec(provider)
    status = account_status(spec.id)
    if not status.get("linked") or not status.get("installed"):
        return []
    if spec.id == "chatgpt":
        if not status.get("authenticated"):
            return []
        try:
            with CodexAppServer(timeout=12) as server:
                raw_models = server.model_list()
        except Exception:
            return []
        result: list[dict[str, Any]] = []
        for item in raw_models:
            model = str(item.get("model") or item.get("id") or "").strip()
            if not model:
                continue
            result.append({
                "provider": spec.id,
                "model": model,
                "id": account_model_id(spec.id, model),
                "display": str(item.get("displayName") or model),
                "default_reasoning_effort": item.get("defaultReasoningEffort"),
                "supported_reasoning_efforts": item.get("supportedReasoningEfforts") or [],
                "is_default": bool(item.get("isDefault")),
            })
        return result
    # Claude/Mistral/Gemini do not currently expose a stable account-specific
    # model-list RPC through their documented account-login CLI surface.  Use
    # only model aliases/IDs documented by the provider; the CLI performs the
    # final entitlement check on invocation.
    if spec.auth_verifiable and not status.get("authenticated"):
        return []
    return [
        {"provider": spec.id, "model": model, "id": account_model_id(spec.id, model), "display": label}
        for model, label in spec.models
    ]


def linked_account_catalog() -> dict[str, Any]:
    providers: list[dict[str, Any]] = []
    models: list[dict[str, Any]] = []
    for status in account_statuses():
        if not status.get("linked"):
            continue
        entry = dict(status)
        entry_models = available_account_models(str(status["provider"]))
        entry["models"] = entry_models
        providers.append(entry)
        models.extend(entry_models)
    return {"providers": providers, "models": models}


def _launch_terminal(command: list[str], *, title: str, wait: bool = False, timeout: int = 360) -> int | None:
    """Launch an official provider's interactive login without handling credentials.

    For synchronous login flows a temporary completion sentinel is written by
    the shell *after* the provider command exits.  This is more reliable than
    waiting for the terminal process because desktop terminals may delegate a
    new tab/window over D-Bus and exit immediately.
    """
    joined = shlex.join(command)
    sentinel = ""
    if wait:
        fd, sentinel = tempfile.mkstemp(prefix="aicoder-login-", suffix=".done")
        os.close(fd)
        try:
            os.unlink(sentinel)
        except OSError:
            pass
        script = (
            f"{joined}; rc=$?; printf '%s' \"$rc\" > {shlex.quote(sentinel)}; "
            f"exit \"$rc\""
        )
    else:
        script = joined + "; exec bash"

    candidates: list[list[str]] = []
    if shutil.which("konsole"):
        candidates.append(["konsole", "--new-tab", "-p", f"tabtitle={title}", "-e", "bash", "-lc", script])
    if shutil.which("gnome-terminal"):
        candidates.append(["gnome-terminal", "--title", title, "--", "bash", "-lc", script])
    if shutil.which("xfce4-terminal"):
        candidates.append(["xfce4-terminal", "--title", title, "-e", f"bash -lc {shlex.quote(script)}"])
    if shutil.which("x-terminal-emulator"):
        candidates.append(["x-terminal-emulator", "-T", title, "-e", "bash", "-lc", script])
    if shutil.which("xterm"):
        candidates.append(["xterm", "-T", title, "-e", "bash", "-lc", script])

    launched = False
    for argv in candidates:
        try:
            subprocess.Popen(argv, start_new_session=True)
            launched = True
            break
        except OSError:
            continue
    if not launched:
        if sentinel:
            try:
                os.unlink(sentinel)
            except OSError:
                pass
        raise ClientError(f"No graphical terminal found. Run manually: {joined}")
    if not wait:
        return None

    deadline = time.monotonic() + max(30, int(timeout))
    try:
        while time.monotonic() < deadline:
            if os.path.exists(sentinel):
                try:
                    text = Path(sentinel).read_text(encoding="utf-8").strip()
                    return int(text) if text else 1
                except (OSError, ValueError):
                    return 1
            time.sleep(0.25)
    finally:
        try:
            os.unlink(sentinel)
        except OSError:
            pass
    raise ClientError(f"{title} timed out before the login process completed")


def _gemini_authenticated(executable: str, *, timeout: int = 45) -> bool:
    """Verify Gemini CLI authentication through its documented headless mode."""
    with tempfile.TemporaryDirectory(prefix="aicoder-gemini-auth-") as tmp:
        policy = Path(tmp) / "deny-tools.toml"
        policy.write_text(
            '[[rule]]\ntoolName = "*"\ndecision = "deny"\npriority = 999\ninteractive = false\n',
            encoding="utf-8",
        )
        env = dict(os.environ)
        env["PATH"] = _augmented_path()
        try:
            proc = subprocess.run(
                [executable, "--prompt", "Reply exactly: OK", "--output-format", "json",
                 "--approval-mode", "plan", "--admin-policy", str(policy), "--skip-trust"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout, cwd=tmp, env=env,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if proc.returncode != 0:
            return False
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return False
        return bool(str(payload.get("response") or "").strip()) if isinstance(payload, dict) else False


def connect_account(provider: str, *, open_browser: bool = True) -> dict[str, Any]:
    spec = provider_spec(provider)
    executable = ensure_provider_client(spec.id)
    if spec.id == "chatgpt":
        # Reuse a valid official Codex/ChatGPT session without forcing another
        # browser round-trip. Otherwise start the App Server OAuth flow.
        try:
            with CodexAppServer(timeout=15) as server:
                existing = server.account_read().get("account")
            if isinstance(existing, dict) and existing.get("type") == "chatgpt":
                set_provider_linked(spec.id, True)
                return {"provider": spec.id, "started": False, "authenticated": True, "account": existing}
        except Exception:
            pass
        # Preferred integration: official Codex App Server ChatGPT OAuth.
        try:
            with CodexAppServer(timeout=30) as server:
                result = server.login_chatgpt(timeout=300, open_browser=open_browser)
            set_provider_linked(spec.id, True)
            return {"provider": spec.id, "started": True, "authenticated": True, "account": result.get("account")}
        except ClientError as primary_error:
            # Official Codex CLI exposes device auth specifically for environments
            # where the localhost browser callback is unavailable/unreliable.
            exit_code = _launch_terminal(
                [executable, "login", "--device-auth"], title="AICoder · ChatGPT Device Login", wait=True
            )
            if exit_code not in (0, None):
                raise primary_error
            try:
                with CodexAppServer(timeout=15) as server:
                    account = server.account_read().get("account")
            except Exception as exc:
                raise ClientError("ChatGPT device login finished but Codex still reports no authenticated account") from exc
            if not isinstance(account, dict) or account.get("type") != "chatgpt":
                raise ClientError("ChatGPT device login did not produce an authenticated ChatGPT account")
            set_provider_linked(spec.id, True)
            return {"provider": spec.id, "started": True, "authenticated": True, "account": account}
    if spec.id == "claude":
        exit_code = _launch_terminal([executable, "auth", "login"], title="AICoder · Claude Login", wait=True)
        status = _claude_status()
        if exit_code not in (0, None) or not status.get("authenticated"):
            raise ClientError("Claude login finished but Claude Code does not report an authenticated account")
        set_provider_linked(spec.id, True)
        return {"provider": spec.id, "started": True, "authenticated": True}
    if spec.id == "mistral":
        exit_code = _launch_terminal([executable, "--setup"], title="AICoder · Mistral Login", wait=True)
        if exit_code not in (0, None):
            raise ClientError("Mistral Vibe setup did not complete successfully")
        set_provider_linked(spec.id, True)
        return {"provider": spec.id, "started": True, "authenticated": None}
    if spec.id == "gemini":
        # Gemini CLI performs Google OAuth in interactive mode; wait for the
        # user to finish/exit, then verify with the documented headless mode.
        exit_code = _launch_terminal([executable], title="AICoder · Google Gemini Login", wait=True)
        if exit_code not in (0, None) or not _gemini_authenticated(executable):
            set_provider_linked(spec.id, False)
            raise ClientError("Gemini login finished but the official Gemini CLI is not authenticated")
        set_provider_linked(spec.id, True)
        return {"provider": spec.id, "started": True, "authenticated": True}
    raise ClientError(f"Unsupported account provider: {spec.id}")


def disconnect_account(provider: str) -> None:
    spec = provider_spec(provider)
    executable = _which(spec)
    if spec.id == "chatgpt" and executable:
        try:
            with CodexAppServer(timeout=10) as server:
                server.logout()
        finally:
            set_provider_linked(spec.id, False)
        return
    if spec.id == "claude" and executable:
        try:
            subprocess.run([executable, "auth", "logout"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        finally:
            set_provider_linked(spec.id, False)
        return
    # Mistral Vibe and Gemini CLI do not expose a stable provider-account logout
    # command in the documented surfaces used here.  Disconnecting therefore
    # only removes AICoder's non-secret linkage and never deletes provider files.
    set_provider_linked(spec.id, False)


_MODEL_BACKEND_SYSTEM = (
    "You are the language-model backend for AILinux AICoder. The conversation below is authoritative. "
    "Do not inspect the machine, repository, network, provider tools, memories, skills, MCP servers, or files on your own. "
    "AICoder owns all tool execution. Return only the next assistant message. If AICoder's system message defines a textual "
    "tool-call protocol, follow that protocol exactly and wait for AICoder to execute the requested tool."
)


def _conversation_text(*, message: str = "", messages: list | None = None, system_prompt: str | None = None) -> str:
    parts = [_MODEL_BACKEND_SYSTEM]
    if system_prompt:
        parts.append(f"\n[system]\n{system_prompt}")
    for item in messages or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "unknown")
        content = item.get("content", "")
        if isinstance(content, str):
            text = content
        else:
            text = json.dumps(content, ensure_ascii=False, default=str)
        parts.append(f"\n[{role}]\n{text}")
    if message and not messages:
        parts.append(f"\n[user]\n{message}")
    parts.append("\n[assistant]\n")
    return "\n".join(parts)


class _SubprocessAccountTransport:
    provider = ""

    def __init__(self, *, timeout: int = 300):
        self.timeout = max(10, min(300, int(timeout)))
        self._active_lock = threading.Lock()
        self._active: dict[str, subprocess.Popen[str]] = {}

    def _run(self, argv: list[str], *, request_id: str | None = None, cwd: str | None = None,
             env: dict[str, str] | None = None, stdin: str | None = None) -> tuple[str, str]:
        key = str(request_id or f"thread-{threading.get_ident()}")
        try:
            proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                cwd=cwd, env=env,
            )
        except OSError as exc:
            raise ClientError(f"Could not start official {self.provider} client") from exc
        with self._active_lock:
            self._active[key] = proc
        try:
            try:
                stdout, stderr = proc.communicate(input=stdin, timeout=self.timeout)
            except subprocess.TimeoutExpired as exc:
                proc.kill()
                proc.communicate()
                raise ClientError(f"{self.provider} account request timed out", retryable=True) from exc
        finally:
            with self._active_lock:
                if self._active.get(key) is proc:
                    self._active.pop(key, None)
        if proc.returncode != 0:
            # Do not copy provider stderr into AICoder errors: login diagnostics
            # can contain authorization URLs or other authentication material.
            raise ClientError(f"{self.provider} account client failed (exit {proc.returncode}); verify the linked account")
        return stdout, stderr

    def cancel_current_request(self, request_id: str | None = None) -> bool:
        with self._active_lock:
            if request_id:
                procs = [self._active.pop(str(request_id), None)]
            else:
                procs = list(self._active.values())
                self._active.clear()
        cancelled = False
        for proc in procs:
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                    cancelled = True
                except OSError:
                    pass
        return cancelled


class ClaudeAccountTransport(_SubprocessAccountTransport):
    provider = "claude"

    def chat(self, message: str = "", model: str | None = None, system_prompt: str | None = None,
             temperature: float = 0.7, max_tokens: int = 4096, fallback_model: str | None = None,
             messages: list | None = None, tools: list | None = None, tool_choice: Any = "auto",
             request_id: str | None = None, reasoning_effort: str | None = None) -> dict[str, Any]:
        provider, provider_model = parse_account_model(model)
        if provider != self.provider:
            raise ClientError("Claude account transport received the wrong provider")
        executable = shutil.which("claude")
        if not executable:
            raise ClientError("Claude Code CLI is not installed")
        transcript = _conversation_text(message=message, messages=messages, system_prompt=system_prompt)
        args = [
            executable, "--print", "--output-format", "text", "--model", provider_model,
            "--tools", "", "--disallowed-tools", "*", "--disable-slash-commands",
            "--no-chrome", "--no-session-persistence", "--system-prompt", _MODEL_BACKEND_SYSTEM,
        ]
        started = time.monotonic()
        stdout, _ = self._run(args, request_id=request_id, stdin=transcript)
        text = stdout.strip()
        if not text:
            raise ClientError("Claude account client returned an empty response", retryable=True)
        elapsed = time.monotonic() - started
        return {"response": text, "model": str(model), "provider": self.provider,
                "backend": "account-claude", "latency_ms": int(elapsed * 1000),
                "_transport_telemetry": {"transport": "account-claude", "elapsed_s": round(elapsed, 3), "request_id": request_id or ""}}


class MistralAccountTransport(_SubprocessAccountTransport):
    provider = "mistral"

    def chat(self, message: str = "", model: str | None = None, system_prompt: str | None = None,
             temperature: float = 0.7, max_tokens: int = 4096, fallback_model: str | None = None,
             messages: list | None = None, tools: list | None = None, tool_choice: Any = "auto",
             request_id: str | None = None, reasoning_effort: str | None = None) -> dict[str, Any]:
        provider, provider_model = parse_account_model(model)
        if provider != self.provider:
            raise ClientError("Mistral account transport received the wrong provider")
        executable = shutil.which("vibe")
        if not executable:
            raise ClientError("Mistral Vibe CLI is not installed")
        transcript = _conversation_text(message=message, messages=messages, system_prompt=system_prompt)
        with tempfile.TemporaryDirectory(prefix="aicoder-vibe-") as tmp:
            args = [executable, "--prompt", transcript, "--max-turns", "1", "--output", "text",
                    "--disabled-tools", "*", "--workdir", tmp, "--trust"]
            env = dict(os.environ)
            # VIBE_* is the documented environment override surface.  Do not set
            # VIBE_HOME: the official client's existing account credentials live
            # there and must remain provider-owned.
            env["VIBE_ACTIVE_MODEL"] = provider_model
            try:
                help_text = subprocess.run([executable, "--help"], capture_output=True, text=True, timeout=5).stdout
            except Exception:
                help_text = ""
            if "--model" in help_text:
                args[1:1] = ["--model", provider_model]
            started = time.monotonic()
            stdout, _ = self._run(args, request_id=request_id, cwd=tmp, env=env)
        text = stdout.strip()
        if not text:
            raise ClientError("Mistral account client returned an empty response", retryable=True)
        elapsed = time.monotonic() - started
        return {"response": text, "model": str(model), "provider": self.provider,
                "backend": "account-mistral", "latency_ms": int(elapsed * 1000),
                "_transport_telemetry": {"transport": "account-mistral", "elapsed_s": round(elapsed, 3), "request_id": request_id or ""}}


class GeminiAccountTransport(_SubprocessAccountTransport):
    provider = "gemini"

    def chat(self, message: str = "", model: str | None = None, system_prompt: str | None = None,
             temperature: float = 0.7, max_tokens: int = 4096, fallback_model: str | None = None,
             messages: list | None = None, tools: list | None = None, tool_choice: Any = "auto",
             request_id: str | None = None, reasoning_effort: str | None = None) -> dict[str, Any]:
        provider, provider_model = parse_account_model(model)
        if provider != self.provider:
            raise ClientError("Gemini account transport received the wrong provider")
        executable = shutil.which("gemini")
        if not executable:
            raise ClientError("Gemini CLI is not installed")
        transcript = _conversation_text(message=message, messages=messages, system_prompt=system_prompt)
        with tempfile.TemporaryDirectory(prefix="aicoder-gemini-") as tmp:
            policy = Path(tmp) / "deny-aicoder-tools.toml"
            policy.write_text(
                '[[rule]]\ntoolName = "*"\ndecision = "deny"\npriority = 999\ninteractive = false\n'
                'denyMessage = "AICoder owns tool execution for this model call."\n',
                encoding="utf-8",
            )
            settings = Path(tmp) / "system-settings.json"
            settings.write_text(json.dumps({"adminPolicyPaths": [str(policy)]}), encoding="utf-8")
            env = dict(os.environ)
            env["GEMINI_CLI_SYSTEM_SETTINGS_PATH"] = str(settings)
            args = [executable, "--prompt", transcript, "--model", provider_model, "--output-format", "json"]
            started = time.monotonic()
            stdout, _ = self._run(args, request_id=request_id, cwd=tmp, env=env)
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise ClientError("Gemini account client returned invalid JSON") from exc
        text = str(payload.get("response") or "").strip() if isinstance(payload, dict) else ""
        if not text:
            raise ClientError("Gemini account client returned an empty response", retryable=True)
        elapsed = time.monotonic() - started
        return {"response": text, "model": str(model), "provider": self.provider,
                "backend": "account-gemini", "latency_ms": int(elapsed * 1000),
                "_transport_telemetry": {"transport": "account-gemini", "elapsed_s": round(elapsed, 3), "request_id": request_id or ""}}


class ChatGPTAccountTransport:
    provider = "chatgpt"

    def __init__(self, *, timeout: int = 300):
        self.timeout = max(10, min(300, int(timeout)))
        self._active_lock = threading.Lock()
        self._active: dict[str, CodexAppServer] = {}

    def cancel_current_request(self, request_id: str | None = None) -> bool:
        with self._active_lock:
            if request_id:
                servers = [self._active.pop(str(request_id), None)]
            else:
                servers = list(self._active.values())
                self._active.clear()
        cancelled = False
        for server in servers:
            if server is not None:
                server.close()
                cancelled = True
        return cancelled

    def chat(self, message: str = "", model: str | None = None, system_prompt: str | None = None,
             temperature: float = 0.7, max_tokens: int = 4096, fallback_model: str | None = None,
             messages: list | None = None, tools: list | None = None, tool_choice: Any = "auto",
             request_id: str | None = None, reasoning_effort: str | None = None) -> dict[str, Any]:
        provider, provider_model = parse_account_model(model)
        if provider != self.provider:
            raise ClientError("ChatGPT account transport received the wrong provider")
        transcript = _conversation_text(message=message, messages=messages, system_prompt=system_prompt)
        key = str(request_id or f"thread-{threading.get_ident()}")
        started = time.monotonic()
        thread_id = ""
        server = CodexAppServer(timeout=min(30, self.timeout))
        with self._active_lock:
            self._active[key] = server
        try:
            account = server.account_read().get("account")
            if not isinstance(account, dict) or account.get("type") != "chatgpt":
                raise ClientError("ChatGPT account is not linked in Codex; reconnect it in AICoder Settings")
            with tempfile.TemporaryDirectory(prefix="aicoder-codex-") as tmp:
                start = server._request("thread/start", {
                    "model": provider_model,
                    "cwd": tmp,
                    "approvalPolicy": "never",
                    "sandbox": "readOnly",
                    "serviceName": "ailinux_aicoder",
                }, timeout=min(30, self.timeout))
                thread = start.get("thread") if isinstance(start.get("thread"), dict) else {}
                thread_id = str(thread.get("id") or "")
                if not thread_id:
                    raise ClientError("Codex App Server did not create a thread")
                turn_params: dict[str, Any] = {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": transcript}],
                    "cwd": tmp,
                    "approvalPolicy": "never",
                    "sandboxPolicy": {
                        "type": "readOnly",
                        "access": {"type": "restricted", "includePlatformDefaults": False, "readableRoots": [tmp]},
                    },
                    "model": provider_model,
                }
                if reasoning_effort:
                    turn_params["effort"] = str(reasoning_effort)
                server._request("turn/start", turn_params, timeout=min(30, self.timeout))
                deadline = time.monotonic() + self.timeout
                answer = ""
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ClientError("ChatGPT account request timed out", retryable=True)
                    event = server._receive(timeout=remaining)
                    # Server-initiated approval/tool requests are never delegated
                    # to Codex. AICoder is the sole tool executor.
                    if "id" in event and event.get("method"):
                        raise ClientError("Codex requested provider-side execution; AICoder account transport refused it")
                    method = str(event.get("method") or "")
                    params = event.get("params") if isinstance(event.get("params"), dict) else {}
                    if method in {"item/started", "item/completed"}:
                        item = params.get("item") if isinstance(params.get("item"), dict) else {}
                        item_type = str(item.get("type") or "")
                        if item_type == "agentMessage" and method == "item/completed":
                            text = str(item.get("text") or "").strip()
                            if text:
                                answer = text
                        elif item_type and item_type not in {"userMessage", "reasoning", "agentMessage"}:
                            raise ClientError(f"Codex attempted provider-side item '{item_type}'; AICoder refused it")
                    if method == "turn/completed":
                        turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                        status = str(turn.get("status") or "")
                        if status != "completed":
                            raise ClientError(f"ChatGPT account turn ended with status {status or 'unknown'}")
                        break
                if not answer:
                    raise ClientError("ChatGPT account client returned an empty response", retryable=True)
        finally:
            if thread_id:
                try:
                    server._request("thread/delete", {"threadId": thread_id}, timeout=5)
                except Exception:
                    pass
            with self._active_lock:
                if self._active.get(key) is server:
                    self._active.pop(key, None)
            server.close()
        elapsed = time.monotonic() - started
        return {"response": answer, "model": str(model), "provider": self.provider,
                "backend": "account-chatgpt", "latency_ms": int(elapsed * 1000),
                "_transport_telemetry": {"transport": "account-chatgpt", "elapsed_s": round(elapsed, 3), "request_id": request_id or ""}}


class _UnavailableModelBackend:
    """Fail-closed default used when only an account transport is available."""

    def __init__(self, timeout: int = 300):
        self.timeout = int(timeout)

    def chat(self, **_kwargs: Any) -> dict[str, Any]:
        raise ClientError("No API/TriForce model backend is configured for this request")

    def cancel_current_request(self, request_id: str | None = None) -> bool:
        return False


def standalone_account_transport(*, timeout: int = 300) -> "AccountRoutingTransport":
    return AccountRoutingTransport(_UnavailableModelBackend(timeout))


class AccountRoutingTransport:
    """Intercept account model IDs and route them fail-closed to official clients."""

    def __init__(self, default: Any):
        self.default = default
        self.timeout = int(getattr(default, "timeout", 300))
        self._transports: dict[str, Any] = {}

    def _transport(self, provider: str) -> Any:
        if provider in self._transports:
            return self._transports[provider]
        classes = {
            "chatgpt": ChatGPTAccountTransport,
            "claude": ClaudeAccountTransport,
            "mistral": MistralAccountTransport,
            "gemini": GeminiAccountTransport,
        }
        transport = classes[provider](timeout=self.timeout)
        self._transports[provider] = transport
        return transport

    def chat(self, **kwargs: Any) -> dict[str, Any]:
        model = kwargs.get("model")
        if not is_account_model(model):
            return self.default.chat(**kwargs)
        provider, _ = parse_account_model(model)
        # Deliberately no try/fallback here. Account IDs must never escape to the
        # API/TriForce backend on authentication, entitlement, or provider errors.
        return self._transport(provider).chat(**kwargs)

    def cancel_current_request(self, request_id: str | None = None) -> bool:
        cancelled = False
        for transport in self._transports.values():
            fn = getattr(transport, "cancel_current_request", None)
            if callable(fn):
                try:
                    cancelled = bool(fn(request_id)) or cancelled
                except Exception:
                    pass
        fn = getattr(self.default, "cancel_current_request", None)
        if callable(fn):
            try:
                cancelled = bool(fn(request_id)) or cancelled
            except Exception:
                pass
        return cancelled

    def __getattr__(self, name: str) -> Any:
        return getattr(self.default, name)
