"""Secure per-provider credentials for direct model transports.

Secrets are stored only in the operating system keyring.  AICoder state,
settings, histories and journals contain provider names/status only, never
credential values.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

try:  # imported lazily enough that headless/test environments can report cleanly
    import keyring  # type: ignore
    from keyring.errors import KeyringError  # type: ignore
    _KEYRING_IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover - exercised through availability behavior
    keyring = None
    _KEYRING_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

    class KeyringError(Exception):
        pass

try:  # Direct Secret Service fallback for frozen Linux builds / plugin discovery failures.
    import secretstorage  # type: ignore
    _SECRETSTORAGE_IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover - platform dependent
    secretstorage = None
    _SECRETSTORAGE_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

from .providers import PROVIDERS

SERVICE_NAME = "ailinux.aicoder.provider-credentials"


class CredentialStoreError(RuntimeError):
    """Raised when the OS secret store cannot safely service a request."""


@dataclass(frozen=True)
class DirectProviderSpec:
    id: str
    aliases: tuple[str, ...]
    base_url: str | None
    direct_supported: bool = True


# Most endpoints below implement the OpenAI Chat Completions request shape.
# Anthropic is routed through the native Messages adapter in model_transport.py.
DIRECT_PROVIDERS: tuple[DirectProviderSpec, ...] = (
    DirectProviderSpec("openai", (), "https://api.openai.com/v1"),
    DirectProviderSpec("google", ("gemini",), "https://generativelanguage.googleapis.com/v1beta/openai"),
    DirectProviderSpec("openrouter", (), "https://openrouter.ai/api/v1"),
    DirectProviderSpec("mistral", ("codestral",), "https://api.mistral.ai/v1"),
    DirectProviderSpec("groq", (), "https://api.groq.com/openai/v1"),
    DirectProviderSpec("xai", ("grok",), "https://api.x.ai/v1"),
    DirectProviderSpec("cerebras", (), "https://api.cerebras.ai/v1"),
    DirectProviderSpec("nvidia", (), "https://integrate.api.nvidia.com/v1"),
    DirectProviderSpec("anthropic", (), "https://api.anthropic.com/v1"),
    DirectProviderSpec("ollama", (), "http://127.0.0.1:11434/v1"),
)


def canonical_provider(name: str) -> str:
    raw = str(name or "").strip().lower()
    for spec in PROVIDERS:
        if raw == spec.id or raw in spec.aliases:
            return spec.id
    return raw


def provider_for_model(model: str | None) -> str:
    raw = str(model or "").strip()
    if "/" not in raw:
        return ""
    return canonical_provider(raw.split("/", 1)[0])


def direct_provider_spec(provider: str) -> DirectProviderSpec | None:
    canonical = canonical_provider(provider)
    for spec in DIRECT_PROVIDERS:
        if canonical == spec.id or canonical in spec.aliases:
            return spec
    return None


def transport_model_id(model: str, provider: str | None = None) -> str:
    """Strip only AICoder's outer provider namespace for a matching provider."""
    raw = str(model or "").strip()
    if "/" not in raw:
        return raw
    prefix, remainder = raw.split("/", 1)
    expected = canonical_provider(provider or prefix)
    if canonical_provider(prefix) == expected:
        return remainder
    return raw


def _provider_env_vars(provider: str) -> tuple[str, ...]:
    canonical = canonical_provider(provider)
    for spec in PROVIDERS:
        if canonical == spec.id:
            return tuple(spec.credential_vars) + tuple(spec.legacy_vars)
    return ()


class _SecretServiceBackend:
    """Minimal Secret Service backend independent of keyring's plugin loader.

    PyInstaller can successfully bundle ``secretstorage`` even in cases where
    ``keyring`` itself cannot discover/load a usable backend at runtime.  This
    wrapper keeps secrets in the desktop's org.freedesktop.secrets service and
    never falls back to plaintext files.
    """

    priority = 1

    @staticmethod
    def _attributes(service: str, account: str) -> dict[str, str]:
        return {
            "application": "ailinux.aicoder",
            "service": str(service),
            "account": str(account),
        }

    @staticmethod
    def _connection():
        if secretstorage is None:
            raise CredentialStoreError("SecretStorage is unavailable")
        try:
            connection = secretstorage.dbus_init()
            if not secretstorage.check_service_availability(connection):
                connection.close()
                raise CredentialStoreError("Desktop Secret Service is not available on the session D-Bus")
            return connection
        except CredentialStoreError:
            raise
        except Exception as exc:
            raise CredentialStoreError(f"Desktop Secret Service unavailable: {type(exc).__name__}") from exc

    @staticmethod
    def _unlocked_collection(connection):
        try:
            collection = secretstorage.get_default_collection(connection)
            if collection.is_locked():
                collection.unlock()
            if collection.is_locked():
                raise CredentialStoreError("Desktop secret collection is locked")
            return collection
        except CredentialStoreError:
            raise
        except Exception as exc:
            raise CredentialStoreError(f"Desktop secret collection unavailable: {type(exc).__name__}") from exc

    def get_password(self, service: str, account: str) -> str | None:
        connection = self._connection()
        try:
            collection = self._unlocked_collection(connection)
            items = list(collection.search_items(self._attributes(service, account)))
            if not items:
                return None
            item = items[0]
            if item.is_locked():
                item.unlock()
            return item.get_secret().decode("utf-8")
        except CredentialStoreError:
            raise
        except Exception as exc:
            raise CredentialStoreError(f"Could not read credential from Desktop Secret Service: {type(exc).__name__}") from exc
        finally:
            connection.close()

    def set_password(self, service: str, account: str, value: str) -> None:
        connection = self._connection()
        try:
            collection = self._unlocked_collection(connection)
            collection.create_item(
                f"AICoder · {account}",
                self._attributes(service, account),
                str(value).encode("utf-8"),
                replace=True,
            )
        except CredentialStoreError:
            raise
        except Exception as exc:
            raise CredentialStoreError(f"Could not store credential in Desktop Secret Service: {type(exc).__name__}") from exc
        finally:
            connection.close()

    def delete_password(self, service: str, account: str) -> None:
        connection = self._connection()
        try:
            collection = self._unlocked_collection(connection)
            for item in list(collection.search_items(self._attributes(service, account))):
                if item.is_locked():
                    item.unlock()
                item.delete()
        except CredentialStoreError:
            raise
        except Exception as exc:
            raise CredentialStoreError(f"Could not delete credential from Desktop Secret Service: {type(exc).__name__}") from exc
        finally:
            connection.close()


def _usable_keyring_backend():
    if keyring is None:
        return None
    try:
        backend = keyring.get_keyring()
    except Exception:
        return None
    priority = getattr(backend, "priority", 0)
    try:
        usable = float(priority) > 0
    except Exception:
        usable = bool(priority)
    return backend if usable else None


def _secretservice_backend():
    if secretstorage is None:
        return None
    backend = _SecretServiceBackend()
    connection = None
    try:
        connection = backend._connection()
        return backend
    except CredentialStoreError:
        return None
    finally:
        if connection is not None:
            connection.close()


def _backend_or_error():
    backend = _usable_keyring_backend()
    if backend is not None:
        return backend
    backend = _secretservice_backend()
    if backend is not None:
        return backend

    details: list[str] = []
    if keyring is None:
        details.append("Python keyring import failed")
    else:
        details.append("Python keyring has no usable backend")
    if secretstorage is None:
        details.append("SecretStorage import failed")
    else:
        details.append("Desktop Secret Service is unavailable")
    raise CredentialStoreError("; ".join(details) + "; refusing plaintext credential storage")


def credential_store_status() -> dict[str, object]:
    """Secret-free diagnostics for the effective OS credential backend."""
    try:
        backend = _backend_or_error()
    except CredentialStoreError as exc:
        return {
            "available": False,
            "backend": "",
            "error": str(exc),
            "keyring_imported": keyring is not None,
            "secretstorage_imported": secretstorage is not None,
        }
    backend_name = (
        "secretstorage.SecretService"
        if isinstance(backend, _SecretServiceBackend)
        else f"{type(backend).__module__}.{type(backend).__name__}"
    )
    return {
        "available": True,
        "backend": backend_name,
        "error": "",
        "keyring_imported": keyring is not None,
        "secretstorage_imported": secretstorage is not None,
    }


def _secret_get(service: str, account: str) -> str:
    backend = _backend_or_error()
    try:
        value = backend.get_password(service, account)
        return str(value or "")
    except CredentialStoreError:
        raise
    except Exception as exc:
        raise CredentialStoreError("Could not read credential from OS secret store") from exc


def _secret_set(service: str, account: str, value: str) -> None:
    backend = _backend_or_error()
    try:
        backend.set_password(service, account, str(value))
    except CredentialStoreError:
        raise
    except Exception as exc:
        raise CredentialStoreError("Could not store credential in OS secret store") from exc


def _secret_delete(service: str, account: str) -> bool:
    backend = _backend_or_error()
    try:
        existing = backend.get_password(service, account)
        if existing is None:
            return False
        backend.delete_password(service, account)
        return True
    except CredentialStoreError:
        raise
    except Exception as exc:
        raise CredentialStoreError("Could not delete credential from OS secret store") from exc


def set_provider_key(provider: str, secret: str) -> None:
    canonical = canonical_provider(provider)
    value = str(secret or "").strip()
    if not canonical or not value:
        raise CredentialStoreError("Provider and non-empty API key are required")
    try:
        _secret_set(SERVICE_NAME, canonical, value)
    except CredentialStoreError as exc:
        raise CredentialStoreError(f"Could not store {canonical} credential: {exc}") from exc


def get_stored_provider_key(provider: str) -> str:
    canonical = canonical_provider(provider)
    if not canonical:
        return ""
    try:
        return _secret_get(SERVICE_NAME, canonical)
    except CredentialStoreError:
        return ""


def delete_provider_key(provider: str) -> bool:
    canonical = canonical_provider(provider)
    try:
        return _secret_delete(SERVICE_NAME, canonical)
    except CredentialStoreError as exc:
        raise CredentialStoreError(f"Could not delete {canonical} credential: {exc}") from exc


def provider_api_key(provider: str, *, environ: Mapping[str, str] | None = None) -> tuple[str, str]:
    """Return (secret, source). Stored OS credential takes precedence over env."""
    canonical = canonical_provider(provider)
    stored = get_stored_provider_key(canonical)
    if stored:
        return stored, "keyring"
    env = os.environ if environ is None else environ
    for name in _provider_env_vars(canonical):
        value = str(env.get(name, "") or "").strip()
        if value:
            return value, f"environment:{name}"
    return "", "none"


def credential_summary(provider: str, *, environ: Mapping[str, str] | None = None) -> dict[str, object]:
    """Secret-free status for GUI/CLI diagnostics."""
    canonical = canonical_provider(provider)
    secret, source = provider_api_key(canonical, environ=environ)
    direct = direct_provider_spec(canonical)
    return {
        "provider": canonical,
        "configured": bool(secret),
        "source": source,
        "direct_supported": bool(direct and direct.direct_supported and direct.base_url),
        "credential_value_exposed": False,
    }
