"""Official account-backed model integrations for AICoder.

This module intentionally does *not* extract OAuth/access tokens from provider
clients.  Each provider keeps ownership of its credentials:

* ChatGPT: Codex App Server managed ChatGPT OAuth (`account/login/start`).
* Claude: Claude Code's `claude auth` commands and non-interactive print mode.
* Mistral: Vibe's setup/login and programmatic mode.
* Google: Antigravity CLI (`agy`) Google OAuth and headless mode.

AICoder stores only provider IDs in ``linked_account_providers``.  Account model
IDs use ``account:<provider>/<model>``.  Once such a model is selected routing is
fail-closed: it can never fall through to TriForce or a BYOK API transport.
"""
from __future__ import annotations

import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timedelta
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
        models=(("sonnet", "Claude Sonnet (latest)"), ("opus", "Claude Opus (latest)"),
                ("fable", "Claude Fable (latest)"), ("haiku", "Claude Haiku (latest)")),
    ),
    AccountProviderSpec(
        "mistral", "Mistral", "vibe", auth_verifiable=True,
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
        "gemini", "Google Antigravity", "agy", dynamic_models=True,
    ),
)

_PROVIDER_MAP = {item.id: item for item in ACCOUNT_PROVIDERS}

# Account-to-account rerouting is deliberately separate from model transport
# fallback. A selected account model may never escape to TriForce/BYOK. When a
# provider is known to be temporarily unavailable (currently Antigravity quota),
# the caller may explicitly choose another authenticated account provider before
# starting the request.
_ACCOUNT_REROUTE_ORDER = ("claude", "chatgpt", "mistral", "gemini")
_ACCOUNT_REROUTE_PREFERRED_MODELS = {
    "claude": ("sonnet", "haiku", "opus"),
    "chatgpt": ("gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.5"),
    "mistral": ("mistral-large-latest", "mistral-medium-latest", "codestral-latest"),
    "gemini": ("gemini-3.8-flash-high", "gemini-3.8-flash-medium", "gemini-3.1-pro-low"),
}
_ANTIGRAVITY_QUOTA_RE = re.compile(
    r"RESOURCE_EXHAUSTED.*?Individual quota reached.*?Resets in\s+"
    r"(?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m)?(?:(?P<seconds>\d+(?:\.\d+)?)s)?",
    re.IGNORECASE,
)
_ANTIGRAVITY_LOG_TIME_RE = re.compile(
    r"^[A-Z](?P<month>\d{2})(?P<day>\d{2})\s+"
    r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})(?:\.(?P<micro>\d{1,6}))?"
)


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
    "gemini": ("antigravity-installer",),
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


def _external_client_env(*, base: dict[str, str] | None = None) -> dict[str, str]:
    """Return a clean environment for provider-owned external executables.

    PyInstaller one-file builds inject their temporary extraction directory into
    ``LD_LIBRARY_PATH`` and preserve the previous value in
    ``LD_LIBRARY_PATH_ORIG``. External CLIs must not inherit the bundle-private
    loader path: it can make independent binaries load incompatible libraries
    or hang before their own runtime starts.
    """
    env = dict(os.environ if base is None else base)
    env["PATH"] = _augmented_path()
    had_original = "LD_LIBRARY_PATH_ORIG" in env
    original_ld = env.pop("LD_LIBRARY_PATH_ORIG", None)
    if getattr(sys, "frozen", False) or had_original:
        if original_ld:
            env["LD_LIBRARY_PATH"] = original_ld
        else:
            env.pop("LD_LIBRARY_PATH", None)
    if getattr(sys, "frozen", False):
        # If the provider CLI is itself a PyInstaller bundle, force it to create
        # its own extraction/runtime environment instead of reusing ours.
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def _external_cli_env(*, base: dict[str, str] | None = None) -> dict[str, str]:
    """Compatibility alias for the provider subprocess environment helper."""
    return _external_client_env(base=base)


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
    if recipe[0] == "antigravity-installer":
        curl = _which_executable("curl")
        bash = _which_executable("bash")
        if not curl or not bash:
            raise ClientError("curl and bash are required to install the official Google Antigravity CLI")
        try:
            download = subprocess.run(
                [curl, "-fsSL", "https://antigravity.google/cli/install.sh"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, env=_external_cli_env(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ClientError("Could not download the official Google Antigravity CLI installer") from exc
        if download.returncode != 0 or not download.stdout:
            raise ClientError("Official Google Antigravity CLI installer download failed")
        try:
            proc = subprocess.run(
                [bash], input=download.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300,
                env=_external_cli_env(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ClientError("Could not install the official Google Antigravity CLI") from exc
        if proc.returncode != 0:
            raise ClientError("Official Google Antigravity CLI installation failed")
        installed = _which_executable(spec.executable)
        if not installed:
            raise ClientError("Google Antigravity CLI installed but 'agy' is not on PATH")
        return installed

    runner = _which_executable(recipe[0])
    if not runner:
        dependency = "Node.js/npm" if recipe[0] == "npm" else "uv"
        raise ClientError(f"{dependency} is required to install the official {spec.display_name} client")
    if recipe[0] == "npm":
        argv = [runner, "install", "-g", "--prefix", str(Path.home() / ".local"), recipe[-1]]
    else:
        argv = [runner, *recipe[1:]]
    env = _external_cli_env()
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
                text=True, encoding="utf-8", bufsize=1, env=_external_cli_env(),
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
    """Read the official Claude Code auth status JSON.

    A persisted AICoder linkage is not treated as proof of authentication.
    This avoids the stale "linked but login required" state after credentials
    expire or are removed outside AICoder.
    """
    spec = provider_spec("claude")
    executable = _which(spec)
    if not executable:
        return {"provider": spec.id, "display": spec.display_name, "installed": False,
                "linked": False, "authenticated": False, "detail": "Claude Code fehlt"}
    marked = spec.id in linked_provider_ids()
    payload: dict[str, Any] = {}
    try:
        proc = subprocess.run(
            [executable, "auth", "status", "--json"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=8,
            env=_external_cli_env(),
        )
        if proc.stdout.strip():
            parsed = json.loads(proc.stdout)
            if isinstance(parsed, dict):
                payload = parsed
        authenticated = proc.returncode == 0 and payload.get("loggedIn") is True
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        authenticated = False

    auth_method = str(payload.get("authMethod") or "").strip()
    subscription = str(payload.get("subscriptionType") or "").strip()
    email = str(payload.get("email") or "").strip()
    if authenticated:
        detail_parts = ["Verbunden"]
        if auth_method:
            detail_parts.append(auth_method)
        if subscription:
            detail_parts.append(subscription)
        if email:
            detail_parts.append(email)
        detail = " · ".join(detail_parts)
    else:
        detail = "Nicht angemeldet · Mit Claude verbinden"
        if marked:
            # Do not preserve a stale AICoder linkage after the official Claude
            # client explicitly reports loggedIn=false.
            set_provider_linked(spec.id, False)
    return {
        "provider": spec.id, "display": spec.display_name, "installed": True,
        "linked": authenticated, "authenticated": authenticated, "detail": detail,
        "auth_method": auth_method, "subscription": subscription, "email": email,
    }


def _antigravity_log_event_time(line: str, *, now: datetime) -> datetime | None:
    match = _ANTIGRAVITY_LOG_TIME_RE.match(line)
    if not match:
        return None
    try:
        micro = (match.group("micro") or "").ljust(6, "0")[:6]
        event = now.replace(
            month=int(match.group("month")), day=int(match.group("day")),
            hour=int(match.group("hour")), minute=int(match.group("minute")),
            second=int(match.group("second")), microsecond=int(micro or 0),
        )
    except ValueError:
        return None
    # CLI log lines omit the year. Around New Year, a December entry observed
    # from January belongs to the previous year.
    if event > now + timedelta(days=2):
        try:
            event = event.replace(year=event.year - 1)
        except ValueError:
            return None
    return event


def antigravity_quota_status(
    *, log_dir: str | Path | None = None, now: datetime | None = None, max_logs: int = 5,
) -> dict[str, Any]:
    """Read provider-owned agy logs for an active individual-quota window.

    This is intentionally observational: no probe request is sent, so checking
    health cannot consume quota or stall an agent. agy currently exposes no
    documented quota-status command, but records the authoritative 429 plus its
    reset countdown in its own CLI log.
    """
    current = now or datetime.now().astimezone()
    root = Path(log_dir).expanduser() if log_dir is not None else Path.home() / ".gemini" / "antigravity-cli" / "log"
    if not root.is_dir():
        return {"quota_exhausted": False, "quota_retry_after_seconds": 0, "quota_reset_at": ""}
    try:
        logs = sorted(root.glob("cli-*.log"), key=lambda item: item.stat().st_mtime, reverse=True)[:max_logs]
    except OSError:
        logs = []
    for path in logs:
        try:
            # The useful quota line is near the end; cap reads so a long-lived
            # CLI installation cannot make every preflight expensive.
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 256 * 1024), os.SEEK_SET)
                text = handle.read().decode("utf-8", errors="replace")
        except OSError:
            continue
        for line in reversed(text.splitlines()):
            match = _ANTIGRAVITY_QUOTA_RE.search(line)
            if not match:
                continue
            seconds = (
                int(match.group("hours") or 0) * 3600
                + int(match.group("minutes") or 0) * 60
                + float(match.group("seconds") or 0)
            )
            if seconds <= 0:
                continue
            event_time = _antigravity_log_event_time(line, now=current)
            if event_time is None:
                try:
                    event_time = datetime.fromtimestamp(path.stat().st_mtime, tz=current.tzinfo)
                except OSError:
                    continue
            reset_at = event_time + timedelta(seconds=seconds)
            retry_after = max(0, int((reset_at - current).total_seconds()))
            if retry_after <= 0:
                continue
            return {
                "quota_exhausted": True,
                "quota_retry_after_seconds": retry_after,
                "quota_reset_at": reset_at.isoformat(),
            }
    return {"quota_exhausted": False, "quota_retry_after_seconds": 0, "quota_reset_at": ""}


def _account_status_usable(status: dict[str, Any]) -> bool:
    return bool(
        status.get("installed")
        and status.get("linked")
        and status.get("authenticated") is not False
        and not status.get("quota_exhausted")
    )


def _preferred_account_model(provider: str) -> str:
    spec = provider_spec(provider)
    if spec.models:
        known = [model for model, _label in spec.models]
        for candidate in _ACCOUNT_REROUTE_PREFERRED_MODELS.get(provider, ()):
            if candidate in known:
                return account_model_id(provider, candidate)
        if known:
            return account_model_id(provider, known[0])
        return ""
    # Dynamic providers need their live catalogue. This is reached only after a
    # cheaper authenticated-status check succeeds.
    rows = available_account_models(provider)
    known = {str(row.get("model") or ""): row for row in rows}
    for candidate in _ACCOUNT_REROUTE_PREFERRED_MODELS.get(provider, ()):
        if candidate in known:
            return account_model_id(provider, candidate)
    if rows:
        model = str(rows[0].get("model") or "").strip()
        return account_model_id(provider, model) if model else ""
    return ""


def reroute_account_model_if_unavailable(model: str | None) -> tuple[str | None, dict[str, Any] | None]:
    """Choose another authenticated account provider for known temporary outages.

    Today the only proactive temporary-outage signal is Antigravity's explicit
    individual-quota 429. Authentication/setup failures remain fail-closed so a
    broken login is never hidden.
    """
    if not is_account_model(model):
        return model, None
    provider, _provider_model = parse_account_model(model)
    status = account_status(provider)
    if not status.get("quota_exhausted"):
        return model, None

    for candidate_provider in _ACCOUNT_REROUTE_ORDER:
        if candidate_provider == provider:
            continue
        candidate_status = account_status(candidate_provider)
        if not _account_status_usable(candidate_status):
            continue
        candidate_model = _preferred_account_model(candidate_provider)
        if not candidate_model:
            continue
        return candidate_model, {
            "from_model": str(model),
            "to_model": candidate_model,
            "reason": "quota_exhausted",
            "provider": provider,
            "retry_after_seconds": int(status.get("quota_retry_after_seconds") or 0),
            "reset_at": str(status.get("quota_reset_at") or ""),
        }
    return model, None


def account_status(provider: str) -> dict[str, Any]:
    spec = provider_spec(provider)
    if spec.id == "chatgpt":
        return _chatgpt_status()
    if spec.id == "claude":
        return _claude_status()
    installed = bool(_which(spec))
    marked = spec.id in linked_provider_ids()
    if spec.id == "mistral":
        authenticated = bool(installed and _mistral_authenticated())
        detail = (
            "Verbunden" if authenticated else
            "Mistral Vibe login required" if installed and marked else
            f"{spec.display_name}-CLI fehlt" if not installed else
            "Nicht verbunden"
        )
        return {"provider": spec.id, "display": spec.display_name, "installed": installed,
                "linked": bool(marked or authenticated), "authenticated": authenticated, "detail": detail}
    if spec.id == "gemini":
        authenticated = False
        if installed:
            executable = _which(spec)
            authenticated = bool(executable and _antigravity_authenticated(executable, timeout=8))
        if authenticated and not marked:
            # Provider-owned auth is authoritative in both directions: recover
            # automatically when the user logged in directly with agy.
            set_provider_linked(spec.id, True)
            marked = True
        elif marked and not authenticated:
            # Do not keep a stale AICoder linkage after the provider session
            # disappears outside AICoder.
            set_provider_linked(spec.id, False)
            marked = False
        quota = antigravity_quota_status() if authenticated else {
            "quota_exhausted": False, "quota_retry_after_seconds": 0, "quota_reset_at": "",
        }
        if authenticated and quota.get("quota_exhausted"):
            retry_h = max(1, int(quota.get("quota_retry_after_seconds") or 0) // 3600)
            detail = f"Verbunden · Quota erschöpft · Reset in ~{retry_h}h"
        else:
            detail = (
                "Verbunden" if authenticated else
                f"{spec.display_name}-CLI fehlt" if not installed else
                "Nicht verbunden · Mit Antigravity verbinden"
            )
        return {
            "provider": spec.id, "display": spec.display_name, "installed": installed,
            "linked": bool(authenticated), "authenticated": authenticated, "detail": detail,
            **quota,
        }
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
    if spec.id == "gemini":
        executable = _which(spec)
        if not executable:
            return []
        try:
            rows = _antigravity_models(executable, timeout=20)
        except ClientError:
            return []
        return [
            {"provider": spec.id, "model": row["model"],
             "id": account_model_id(spec.id, row["model"]), "display": row["display"]}
            for row in rows
        ]

    # Claude/Mistral do not currently expose a stable account-specific
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
            subprocess.Popen(argv, start_new_session=True, env=_external_cli_env())
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


def _antigravity_models(executable: str, *, timeout: int = 30) -> list[dict[str, str]]:
    """Return models exposed by the authenticated Antigravity CLI account."""
    env = _external_cli_env()
    try:
        proc = subprocess.run(
            [executable, "models"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=timeout, env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClientError("Could not query Google Antigravity models") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:]
        suffix = f": {detail[0][:200]}" if detail else ""
        raise ClientError(f"Google Antigravity is not authenticated{suffix}")
    models: list[dict[str, str]] = []
    for line in proc.stdout.splitlines():
        raw = line.strip()
        if not raw or raw.lower().startswith("fetching available models"):
            continue
        parts = raw.split(None, 1)
        slug = parts[0].strip()
        if not slug or slug.startswith("-"):
            continue
        display = parts[1].strip() if len(parts) > 1 else slug
        models.append({"model": slug, "display": display})
    return models


def _antigravity_authenticated(executable: str, *, timeout: int = 30) -> bool:
    try:
        _antigravity_models(executable, timeout=timeout)
        return True
    except ClientError:
        return False


def _clean_account_response_text(provider: str, text: str) -> str:
    """Remove provider-CLI diagnostics that can leak into model stdout.

    Keep this deliberately narrow: only strip exact, known transport diagnostics.
    Model content must otherwise remain byte-for-byte intact apart from outer
    whitespace normalization.
    """
    lines = str(text or "").splitlines()
    if provider == "claude":
        lines = [
            line for line in lines
            if not line.strip().startswith(
                "Client.listTools() called but server does not advertise tools capability"
            )
        ]
    return "\n".join(lines).strip()


def _mistral_authenticated() -> bool:
    """Check whether Mistral Vibe can resolve its provider credential.

    Vibe 2.x resolves MISTRAL_API_KEY from the process environment, its
    $VIBE_HOME/.env file, or the provider-owned OS keyring.  Only presence is
    checked here; secret values never leave their storage backend.
    """
    env_key = "MISTRAL_API_KEY"
    if str(os.environ.get(env_key) or "").strip():
        return True

    vibe_home = Path(os.path.expanduser(os.environ.get("VIBE_HOME") or "~/.vibe"))
    env_file = vibe_home / ".env"
    try:
        if env_file.is_file():
            for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                raw = line.strip()
                if not raw or raw.startswith("#") or "=" not in raw:
                    continue
                name, value = raw.split("=", 1)
                if name.strip() == env_key and value.strip().strip("\"'"):
                    return True
    except OSError:
        pass

    try:
        import keyring  # type: ignore
        from keyring.errors import KeyringError  # type: ignore
        for service in ("ai.mistral.vibe", "vibe"):
            try:
                if keyring.get_password(service, env_key):
                    return True
            except KeyringError:
                break
    except Exception:
        pass
    return False


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
        existing = _claude_status()
        if existing.get("authenticated"):
            set_provider_linked(spec.id, True)
            return {"provider": spec.id, "started": False, "authenticated": True, "account": existing}

        # Claude Code may hand browser OAuth off and let `claude auth login`
        # return before the browser callback has updated the local account.
        # Keep the terminal shell open and treat the documented auth-status JSON
        # as the authoritative completion signal instead of terminal exit.
        _launch_terminal(
            [executable, "auth", "login", "--claudeai"],
            title="AICoder · Claude Login",
            wait=False,
        )
        deadline = time.monotonic() + 300
        status: dict[str, Any] = {}
        while time.monotonic() < deadline:
            status = _claude_status()
            if status.get("authenticated"):
                set_provider_linked(spec.id, True)
                return {"provider": spec.id, "started": True, "authenticated": True, "account": status}
            time.sleep(1.0)
        set_provider_linked(spec.id, False)
        raise ClientError(
            "Claude login timed out after 5 minutes. Finish the browser login and press Connect again."
        )
    if spec.id == "mistral":
        exit_code = _launch_terminal([executable, "--setup"], title="AICoder · Mistral Login", wait=True)
        if exit_code not in (0, None):
            raise ClientError("Mistral Vibe setup did not complete successfully")
        set_provider_linked(spec.id, True)
        return {"provider": spec.id, "started": True, "authenticated": None}
    if spec.id == "gemini":
        # Antigravity is a long-lived interactive TUI: successful OAuth does not
        # necessarily make the `agy` process exit. Waiting for the terminal
        # therefore leaves the Settings worker stuck at "Client wird geprüft".
        # Launch it detached and use `agy models` as the authoritative login
        # signal, just like Claude uses its provider-owned auth status.
        if _antigravity_authenticated(executable):
            set_provider_linked(spec.id, True)
            return {"provider": spec.id, "started": False, "authenticated": True}
        _launch_terminal([executable], title="AICoder · Google Antigravity Login", wait=False)
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            if _antigravity_authenticated(executable, timeout=8):
                set_provider_linked(spec.id, True)
                return {"provider": spec.id, "started": True, "authenticated": True}
            time.sleep(1.0)
        set_provider_linked(spec.id, False)
        raise ClientError(
            "Antigravity login timed out after 5 minutes. Finish the Google login and press Connect again."
        )
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
            subprocess.run(
                [executable, "auth", "logout"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=15, env=_external_cli_env(),
            )
        finally:
            set_provider_linked(spec.id, False)
        return
    # Mistral Vibe and Google Antigravity CLI do not expose a stable provider-account logout
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
        executable = _which_executable("claude")
        if not executable:
            raise ClientError("Claude Code CLI is not installed")
        transcript = _conversation_text(message=message, messages=messages, system_prompt=system_prompt)
        args = [
            executable, "--print", "--output-format", "text", "--model", provider_model,
            "--tools", "", "--disallowed-tools", "*", "--disable-slash-commands",
            "--no-chrome", "--no-session-persistence", "--system-prompt", _MODEL_BACKEND_SYSTEM,
        ]
        env = _external_cli_env()
        # Account-backed Claude must use the provider-owned claude.ai session.
        # API/gateway credentials inherited from the TriForce host take precedence
        # in Claude Code and can silently route the request to a depleted API balance.
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
            env.pop(name, None)
        started = time.monotonic()
        stdout, _ = self._run(args, request_id=request_id, stdin=transcript, env=env)
        text = _clean_account_response_text(self.provider, stdout)
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
        executable = _which_executable("vibe")
        if not executable:
            raise ClientError("Mistral Vibe CLI is not installed")
        if not _mistral_authenticated():
            raise ClientError("Mistral Vibe login required")
        transcript = _conversation_text(message=message, messages=messages, system_prompt=system_prompt)
        with tempfile.TemporaryDirectory(prefix="aicoder-vibe-") as tmp:
            args = [executable, "--prompt", transcript, "--max-turns", "1", "--output", "text",
                    "--disabled-tools", "*", "--workdir", tmp, "--trust"]
            env = _external_cli_env()
            # VIBE_* is the documented environment override surface.  Do not set
            # VIBE_HOME: the official client's existing account credentials live
            # there and must remain provider-owned.
            env["VIBE_ACTIVE_MODEL"] = provider_model
            try:
                help_text = subprocess.run(
                    [executable, "--help"], capture_output=True, text=True, timeout=5, env=env
                ).stdout
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
            raise ClientError("Google Antigravity account transport received the wrong provider")
        executable = _which_executable("agy")
        if not executable:
            raise ClientError("Google Antigravity CLI is not installed")
        if not _antigravity_authenticated(executable, timeout=min(8, self.timeout)):
            raise ClientError("Antigravity login required")
        transcript = _conversation_text(message=message, messages=messages, system_prompt=system_prompt)
        with tempfile.TemporaryDirectory(prefix="aicoder-antigravity-") as tmp:
            args = [
                executable, "--print", transcript, "--model", provider_model,
                "--output-format", "json", "--mode", "plan", "--sandbox",
                "--disable-slash-commands", "--print-timeout", f"{self.timeout}s",
            ]
            if reasoning_effort in {"low", "medium", "high"}:
                args.extend(["--effort", str(reasoning_effort)])
            env = _external_cli_env()
            started = time.monotonic()
            stdout, _ = self._run(args, request_id=request_id, cwd=tmp, env=env)
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise ClientError("Google Antigravity returned invalid JSON") from exc
        text = ""
        if isinstance(payload, dict):
            status = str(payload.get("status") or "").strip().upper()
            error = str(payload.get("error") or "").strip()
            if status == "ERROR":
                detail = error[:300] if error else "provider reported an unspecified error"
                raise ClientError(f"Google Antigravity request failed: {detail}", retryable=True)
            text = str(payload.get("response") or payload.get("result") or payload.get("text") or "").strip()
            if not text and isinstance(payload.get("result"), dict):
                text = str(payload["result"].get("response") or "").strip()
        if not text:
            raise ClientError("Google Antigravity returned an empty response", retryable=True)
        elapsed = time.monotonic() - started
        return {"response": text, "model": str(model), "provider": self.provider,
                "backend": "account-antigravity", "latency_ms": int(elapsed * 1000),
                "_transport_telemetry": {"transport": "account-antigravity", "elapsed_s": round(elapsed, 3), "request_id": request_id or ""}}


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
                    "sandbox": "read-only",
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
                    # Codex App Server currently uses different enum spellings:
                    # thread/start.sandbox is kebab-case, turn/start sandboxPolicy.type is camelCase.
                    "sandboxPolicy": {
                        "type": "readOnly",
                        "networkAccess": False,
                    },
                    "model": provider_model,
                }
                if reasoning_effort:
                    turn_params["effort"] = str(reasoning_effort)
                server._request("turn/start", turn_params, timeout=min(30, self.timeout))
                deadline = time.monotonic() + self.timeout
                answer = ""
                provider_error = ""
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
                    if method == "error":
                        error_payload = params.get("error") if isinstance(params.get("error"), dict) else {}
                        message_text = str(error_payload.get("message") or "").strip()
                        error_code = str(error_payload.get("codexErrorInfo") or "").strip()
                        if message_text:
                            provider_error = message_text + (f" [{error_code}]" if error_code else "")
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
                            turn_error = turn.get("error") if isinstance(turn.get("error"), dict) else {}
                            message_text = str(turn_error.get("message") or "").strip()
                            error_code = str(turn_error.get("codexErrorInfo") or "").strip()
                            detail = message_text + (f" [{error_code}]" if message_text and error_code else "")
                            if not detail:
                                detail = provider_error
                            suffix = f": {detail[:500]}" if detail else ""
                            raise ClientError(f"ChatGPT account turn ended with status {status or 'unknown'}{suffix}")
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
