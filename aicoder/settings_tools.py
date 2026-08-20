"""Typed model-facing access to AICoder settings.

The model never edits state.json directly. Read operations expose only registry
metadata/effective non-secret values. Mutations validate through SettingsStore;
security-impacting changes are marked by the executor and must cross the host
approval boundary before this module is called.
"""
from __future__ import annotations

import json
from typing import Any

from . import settings


TOOL_SCHEMAS = [
    {
        "name": "settings_list",
        "description": "List AICoder settings and effective non-secret values.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "settings_describe",
        "description": "Describe one AICoder setting, including choices, bounds and security impact.",
        "inputSchema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    },
    {
        "name": "settings_get",
        "description": "Read one effective AICoder setting. Sensitive values are never returned.",
        "inputSchema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    },
    {
        "name": "settings_plan_patch",
        "description": "Validate a proposed settings patch without changing anything; reports old/new values and security-impacting keys.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "patch": {"type": "object", "additionalProperties": True},
            },
            "required": ["patch"],
        },
    },
    {
        "name": "settings_apply_patch",
        "description": "Apply a validated AICoder settings patch. Security-impacting changes always require explicit host approval.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "patch": {"type": "object", "additionalProperties": True},
                "reason": {"type": "string"},
            },
            "required": ["patch"],
        },
    },
    {
        "name": "settings_reset",
        "description": "Reset one AICoder setting to its schema default. Security-impacting settings require explicit host approval.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["key"],
        },
    },
]

TOOL_NAMES = {item["name"] for item in TOOL_SCHEMAS}
MUTATING_TOOLS = {"settings_apply_patch", "settings_reset"}


def _visible_value(key: str, value: Any) -> Any:
    return "***" if settings.REGISTRY[key].sensitive else value


def _normalized_patch(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or not raw:
        raise settings.SettingsError("patch must be a non-empty object")
    normalized: dict[str, Any] = {}
    for raw_key, raw_value in raw.items():
        key = settings.resolve_key(str(raw_key))
        spec = settings.REGISTRY[key]
        if not spec.mutable:
            raise settings.SettingsError(f"'{key}' is read-only.")
        normalized[key] = settings.coerce(key, raw_value)
    return normalized


def plan_patch(raw: Any) -> dict[str, Any]:
    patch = _normalized_patch(raw)
    current = settings.STORE.load()
    proposed = dict(current)
    proposed.update(patch)
    settings.apply_invariants(proposed)
    changes = []
    for key in sorted(patch):
        spec = settings.REGISTRY[key]
        old = current.get(key, spec.default)
        new = proposed.get(key, spec.default)
        if old == new:
            continue
        changes.append({
            "key": key,
            "old": _visible_value(key, old),
            "new": _visible_value(key, new),
            "security_impact": spec.security_impact,
            "restart_required": spec.restart_required,
        })
    return {
        "changes": changes,
        "security_confirmation_required": any(item["security_impact"] for item in changes),
    }


def security_change_requested(tool_name: str, args: dict[str, Any]) -> bool:
    try:
        if tool_name == "settings_apply_patch":
            return bool(plan_patch(args.get("patch"))["security_confirmation_required"])
        if tool_name == "settings_reset":
            key = settings.resolve_key(str(args.get("key") or ""))
            spec = settings.REGISTRY[key]
            return bool(spec.security_impact and settings.STORE.get(key) != spec.default)
    except settings.SettingsError:
        return False
    return False


def run(tool_name: str, args: dict[str, Any]) -> tuple[str, bool]:
    try:
        if tool_name == "settings_list":
            state = settings.STORE.load()
            rows = []
            for key in sorted(settings.REGISTRY, key=lambda k: (settings.REGISTRY[k].group, k)):
                spec = settings.REGISTRY[key]
                rows.append({
                    "key": key,
                    "value": _visible_value(key, state.get(key, spec.default)),
                    "type": spec.type,
                    "group": spec.group,
                    "choices": spec.choice_list(),
                    "description": spec.description,
                    "security_impact": spec.security_impact,
                })
            return json.dumps(rows, ensure_ascii=False, sort_keys=True), False

        if tool_name == "settings_describe":
            return json.dumps(settings.describe(str(args.get("key") or "")), ensure_ascii=False, sort_keys=True), False

        if tool_name == "settings_get":
            key = settings.resolve_key(str(args.get("key") or ""))
            value = _visible_value(key, settings.STORE.get(key))
            return json.dumps({"key": key, "value": value}, ensure_ascii=False, sort_keys=True), False

        if tool_name == "settings_plan_patch":
            return json.dumps(plan_patch(args.get("patch")), ensure_ascii=False, sort_keys=True), False

        if tool_name == "settings_apply_patch":
            plan = plan_patch(args.get("patch"))
            patch = _normalized_patch(args.get("patch"))
            saved = settings.STORE.update(**patch)
            verified = {
                item["key"]: _visible_value(item["key"], saved.get(item["key"]))
                for item in plan["changes"]
            }
            return json.dumps({"applied": plan["changes"], "verified": verified}, ensure_ascii=False, sort_keys=True), False

        if tool_name == "settings_reset":
            key = settings.resolve_key(str(args.get("key") or ""))
            spec = settings.REGISTRY[key]
            old = settings.STORE.get(key)
            saved = settings.STORE.reset(key)
            result = {
                "key": key,
                "old": _visible_value(key, old),
                "new": _visible_value(key, saved.get(key)),
                "security_impact": spec.security_impact,
            }
            return json.dumps(result, ensure_ascii=False, sort_keys=True), False

        return f"{tool_name}: unknown settings tool", True
    except settings.SettingsError as exc:
        return f"{tool_name} error: {exc}", True
    except Exception as exc:
        return f"{tool_name} error: {type(exc).__name__}: {exc}", True
