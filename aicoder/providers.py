"""Provider/model credential diagnostics without exposing credential values."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable

from .client import model_identifier


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    aliases: tuple[str, ...] = ()
    credential_vars: tuple[str, ...] = ()
    legacy_vars: tuple[str, ...] = ()
    notes: str = ""


PROVIDERS: tuple[ProviderSpec, ...] = (
    ProviderSpec("openai", credential_vars=("OPENAI_API_KEY",)),
    ProviderSpec("anthropic", credential_vars=("ANTHROPIC_API_KEY",), legacy_vars=("ANTHROPIC_AUTH_TOKEN",), notes="ANTHROPIC_AUTH_TOKEN is commonly used for gateways/Claude Code; direct Anthropic API normally uses ANTHROPIC_API_KEY."),
    ProviderSpec("google", aliases=("gemini",), credential_vars=("GOOGLE_API_KEY", "GEMINI_API_KEY"), legacy_vars=("GOOGLE_AI_STUDIO_KEY", "GOOGLE_GEMINI_KEY")),
    ProviderSpec("mistral", aliases=("codestral",), credential_vars=("MISTRAL_API_KEY",), legacy_vars=("MIXTRAL_API_KEY", "CODESTRAL_API_KEY")),
    ProviderSpec("groq", credential_vars=("GROQ_API_KEY",)),
    ProviderSpec("openrouter", credential_vars=("OPENROUTER_API_KEY",)),
    ProviderSpec("cerebras", credential_vars=("CEREBRAS_API_KEY",)),
    ProviderSpec("together", credential_vars=("TOGETHER_API_KEY",)),
    ProviderSpec("cohere", credential_vars=("COHERE_API_KEY",)),
    ProviderSpec("huggingface", aliases=("hugging_face", "hf"), credential_vars=("HF_TOKEN", "HUGGINGFACE_API_KEY")),
    ProviderSpec("fireworks", credential_vars=("FIREWORKS_API_KEY",)),
    ProviderSpec("jina", credential_vars=("JINA_API_KEY",)),
    ProviderSpec("nvidia", credential_vars=("NVIDIA_API_KEY",)),
    ProviderSpec("cloudflare", credential_vars=("CLOUDFLARE_API_TOKEN",), legacy_vars=("CLOUDFLARE_API_KEY",)),
    ProviderSpec("kimi", aliases=("moonshot",), credential_vars=("KIMI_API_KEY", "MOONSHOT_API_KEY")),
    ProviderSpec("ollama", notes="Local Ollama normally requires no API credential."),
)


def _provider_name(entry: Any) -> str:
    if isinstance(entry, dict):
        raw = entry.get("provider") or entry.get("provider_id")
        if isinstance(raw, str) and raw.strip():
            return raw.strip().lower()
    model_id = model_identifier(entry)
    return model_id.split("/", 1)[0].lower() if "/" in model_id else ""


def _backend_counts(models: Iterable[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in models:
        provider = _provider_name(entry)
        if provider:
            counts[provider] = counts.get(provider, 0) + 1
    return counts


def _matches(spec: ProviderSpec, provider: str) -> bool:
    names = {spec.id, *spec.aliases}
    return provider.lower() in names


def _gemini_warning(today: date) -> str:
    if today < date(2026, 9, 1):
        return "Gemini migration: Google says Standard API keys will be rejected starting September 2026; migrate to an Authorization key before then."
    if today < date(2027, 1, 1):
        return "Gemini auth: Standard API keys may now be rejected; use a current Authorization key and verify the configured key in Google AI Studio."
    return ""


def provider_status(client: Any | None = None, *, environ: dict[str, str] | None = None, today: date | None = None) -> list[dict[str, Any]]:
    env = os.environ if environ is None else environ
    now = today or date.today()
    models: list[Any] = []
    backend_error = ""
    if client is not None:
        try:
            models = list(client.list_models() or [])
        except Exception as exc:
            backend_error = f"{type(exc).__name__}: {exc}"[:500]
    counts = _backend_counts(models)
    rows: list[dict[str, Any]] = []
    for spec in PROVIDERS:
        backend_count = sum(count for provider, count in counts.items() if _matches(spec, provider))
        present = [name for name in spec.credential_vars if bool(env.get(name))]
        legacy = [name for name in spec.legacy_vars if bool(env.get(name))]
        warnings: list[str] = []
        if legacy:
            warnings.append("Legacy/compatibility credential variable present: " + ", ".join(legacy))
        if spec.id == "google":
            warning = _gemini_warning(now)
            if warning:
                warnings.append(warning)
            if env.get("GOOGLE_API_KEY") and env.get("GEMINI_API_KEY"):
                warnings.append("Both GOOGLE_API_KEY and GEMINI_API_KEY are present; current Google SDK precedence prefers GOOGLE_API_KEY.")
        rows.append({
            "provider": spec.id,
            "backend_model_count": backend_count,
            "backend_available": backend_count > 0,
            "credential_source": "backend" if backend_count > 0 else ("environment" if present else "none-detected"),
            "environment_variables_present": present,
            "legacy_variables_present": legacy,
            "credential_value_exposed": False,
            "backend_error": backend_error,
            "health_check": "not-run",
            "warnings": warnings,
            "notes": spec.notes,
        })
    known = {name for spec in PROVIDERS for name in (spec.id, *spec.aliases)}
    for provider, count in sorted(counts.items()):
        if provider not in known:
            rows.append({
                "provider": provider,
                "backend_model_count": count,
                "backend_available": True,
                "credential_source": "backend",
                "environment_variables_present": [],
                "legacy_variables_present": [],
                "credential_value_exposed": False,
                "backend_error": backend_error,
                "health_check": "not-run",
                "warnings": [],
                "notes": "Provider discovered from backend model metadata; no local credential rule is registered.",
            })
    return rows


def credential_status(*, environ: dict[str, str] | None = None, today: date | None = None) -> list[dict[str, Any]]:
    """Local credential presence only. Never returns values."""
    return provider_status(None, environ=environ, today=today)
