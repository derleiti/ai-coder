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
except Exception:  # pragma: no cover - exercised through availability behavior
    keyring = None

    class KeyringError(Exception):
        pass

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
    DirectProviderSpec("cerebras", (), "https://api.cerebras.ai/v1"),
    DirectProviderSpec("nvidia", (), "https://integrate.api.nvidia.com/v1"),
    DirectProviderSpec("anthropic", (), "https://api.anthropic.com/v1"),
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


def _backend_or_error():
    if keyring is None:
        raise CredentialStoreError("Python keyring is unavailable; refusing plaintext credential storage")
    try:
        backend = keyring.get_keyring()
    except Exception as exc:
        raise CredentialStoreError(f"OS secret store unavailable: {type(exc).__name__}") from exc
    priority = getattr(backend, "priority", 0)
    try:
        usable = float(priority) > 0
    except Exception:
        usable = bool(priority)
    if not usable:
        raise CredentialStoreError("No usable OS keyring backend is available; credential was not stored")
    return backend


def set_provider_key(provider: str, secret: str) -> None:
    canonical = canonical_provider(provider)
    value = str(secret or "").strip()
    if not canonical or not value:
        raise CredentialStoreError("Provider and non-empty API key are required")
    _backend_or_error()
    try:
        keyring.set_password(SERVICE_NAME, canonical, value)
    except Exception as exc:
        raise CredentialStoreError(f"Could not store {canonical} credential in OS keyring") from exc


def get_stored_provider_key(provider: str) -> str:
    canonical = canonical_provider(provider)
    if not canonical or keyring is None:
        return ""
    try:
        _backend_or_error()
        return str(keyring.get_password(SERVICE_NAME, canonical) or "")
    except CredentialStoreError:
        return ""
    except Exception:
        return ""


def delete_provider_key(provider: str) -> bool:
    canonical = canonical_provider(provider)
    _backend_or_error()
    try:
        existing = keyring.get_password(SERVICE_NAME, canonical)
        if existing is None:
            return False
        keyring.delete_password(SERVICE_NAME, canonical)
        return True
    except Exception as exc:
        # Different backends use different errors for a missing entry; never
        # include backend exception text because it may be provider-specific.
        raise CredentialStoreError(f"Could not delete {canonical} credential from OS keyring") from exc


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
