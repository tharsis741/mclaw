# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Scoped secret allowlist and request helpers."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable
from typing import Any

from mclaw.cli.config import get_env_value, save_env_value
from mclaw.constants import get_mclaw_home
from mclaw.utils import atomic_json_write

ENV_VAR_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
ALLOWLIST_FILENAME = "secret_allowlist.json"
ALLOWLIST_VERSION = 1
SCOPE_BUCKETS = {"skill": "skills", "tool": "tools", "runtime": "runtime", "channel": "channels"}
_EMPTY_ALLOWLIST: dict[str, dict[str, list[str]]] = {
    "skills": {},
    "tools": {},
    "runtime": {},
    "channels": {},
}


class SecretRequestError(ValueError):
    """Raised when a secret request is malformed."""


def allowlist_path():
    """Return the scoped-secret allowlist path under MCLAW_HOME."""
    return get_mclaw_home() / ALLOWLIST_FILENAME


def _blank_allowlist() -> dict[str, Any]:
    return {"version": ALLOWLIST_VERSION, **{bucket: {} for bucket in _EMPTY_ALLOWLIST}}


def _normalize_env_var(value: Any) -> str:
    env_var = str(value or "").strip().upper()
    if not ENV_VAR_RE.match(env_var):
        raise SecretRequestError(f"Invalid environment variable name: {value!r}")
    return env_var


def parse_scope(required_for: str) -> tuple[str, str]:
    """Parse skill/tool/runtime/channel scope ids for secret authorization."""
    raw = str(required_for or "").strip()
    if ":" not in raw:
        raise SecretRequestError("required_for must use one of: skill:<name>, tool:<name>, runtime:<name>, channel:<name>")
    kind, _, scope_id = raw.partition(":")
    kind = kind.strip().lower()
    scope_id = scope_id.strip()
    bucket = SCOPE_BUCKETS.get(kind)
    if not bucket or not scope_id or ":" in scope_id or any(ch.isspace() for ch in scope_id):
        raise SecretRequestError("required_for must use one of: skill:<name>, tool:<name>, runtime:<name>, channel:<name>")
    return bucket, scope_id


def normalize_allowlist(data: Any) -> dict[str, Any]:
    """Normalize persisted allowlist data and drop malformed entries."""
    if not isinstance(data, dict):
        return _blank_allowlist()
    normalized = _blank_allowlist()
    for bucket in _EMPTY_ALLOWLIST:
        scopes = data.get(bucket, {})
        if not isinstance(scopes, dict):
            continue
        for scope_id, values in scopes.items():
            scope_name = str(scope_id or "").strip()
            if not scope_name:
                continue
            if isinstance(values, str):
                raw_values: Iterable[Any] = [values]
            elif isinstance(values, (list, tuple, set)):
                raw_values = values
            else:
                continue
            env_vars: list[str] = []
            for item in raw_values:
                try:
                    env_var = _normalize_env_var(item)
                except SecretRequestError:
                    continue
                if env_var not in env_vars:
                    env_vars.append(env_var)
            normalized[bucket][scope_name] = env_vars
    return normalized


def load_allowlist() -> dict[str, Any]:
    """Load the allowlist, treating missing or invalid data as empty."""
    path = allowlist_path()
    if not path.exists():
        return _blank_allowlist()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("version") != ALLOWLIST_VERSION:
            return _blank_allowlist()
        return normalize_allowlist(raw)
    except Exception:
        return _blank_allowlist()


def save_allowlist(data: dict[str, Any]) -> None:
    """Persist the allowlist atomically with best-effort private permissions."""
    get_mclaw_home().mkdir(parents=True, exist_ok=True)
    atomic_json_write(allowlist_path(), normalize_allowlist(data))
    try:
        os.chmod(allowlist_path(), 0o600)
    except (OSError, NotImplementedError):
        pass


def is_authorized(required_for: str, env_var: str) -> bool:
    """Return whether a scope may receive one environment variable."""
    bucket, scope_id = parse_scope(required_for)
    env_name = _normalize_env_var(env_var)
    scopes = load_allowlist().get(bucket, {})
    return env_name in set(scopes.get(scope_id, []))


def authorize(required_for: str, env_vars: Iterable[str]) -> list[str]:
    """Add environment variables to a scope allowlist without storing values."""
    bucket, scope_id = parse_scope(required_for)
    data = load_allowlist()
    current = list(data.setdefault(bucket, {}).setdefault(scope_id, []))
    changed = False
    authorized: list[str] = []
    for value in env_vars:
        env_var = _normalize_env_var(value)
        if env_var not in current:
            current.append(env_var)
            changed = True
        authorized.append(env_var)
    data[bucket][scope_id] = current
    if changed:
        save_allowlist(data)
    return authorized


def authorized_env_vars(required_for: str) -> list[str]:
    bucket, scope_id = parse_scope(required_for)
    return list(load_allowlist().get(bucket, {}).get(scope_id, []))


def build_scoped_env(required_for: str | None) -> tuple[dict[str, str], set[str]]:
    """Return only authorized secret values and their env names for subprocesses."""
    if not required_for:
        return {}, set()
    env: dict[str, str] = {}
    for env_var in authorized_env_vars(required_for):
        value = get_env_value(env_var)
        if value:
            env[env_var] = value
    return env, set(env)


def redact_secret_values(value: Any, secrets: Iterable[str] | dict[str, str]) -> Any:
    """Replace exact secret values before data can be returned to the model."""
    values = secrets.values() if isinstance(secrets, dict) else secrets
    redactions = sorted(
        {
            str(secret)
            for secret in values
            if isinstance(secret, str) and len(secret) >= 4
        },
        key=len,
        reverse=True,
    )
    if not redactions:
        return value
    if isinstance(value, str):
        redacted = value
        for secret in redactions:
            redacted = redacted.replace(secret, "<redacted>")
        return redacted
    if isinstance(value, list):
        return [redact_secret_values(item, redactions) for item in value]
    if isinstance(value, dict):
        return {key: redact_secret_values(item, redactions) for key, item in value.items()}
    return value


def _normalize_secret_item(item: Any) -> dict[str, str]:
    if isinstance(item, str):
        env_var = _normalize_env_var(item)
        return {"env_var": env_var, "provider": "", "purpose": ""}
    if not isinstance(item, dict):
        raise SecretRequestError("secrets must be strings or objects with env_var")
    if not item.get("env_var"):
        raise SecretRequestError("secret objects must include env_var")
    env_var = _normalize_env_var(item.get("env_var"))
    return {
        "env_var": env_var,
        "provider": str(item.get("provider") or "").strip(),
        "purpose": str(item.get("purpose") or "").strip(),
    }


def secret_request_many(
    required_for: str,
    secrets: list[Any],
    prompt_callback: Callable[[str, list[dict[str, str]]], dict[str, Any] | None] | None = None,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Coordinate secret prompts, storage, and scoped authorization.

    Plaintext values can be saved through config helpers but are never returned
    in the result payload; callers receive only env var names and status lists.
    """
    bucket, _scope_id = parse_scope(required_for)
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in secrets or []:
        secret = _normalize_secret_item(item)
        env_var = secret["env_var"]
        if env_var in seen:
            continue
        seen.add(env_var)
        normalized.append(secret)

    configured: list[str] = []
    authorized_now: list[str] = []
    refreshed: list[str] = []
    skipped: list[str] = []
    missing: list[str] = []
    needs_prompt: list[dict[str, str]] = []

    for secret in normalized:
        env_var = secret["env_var"]
        has_value = bool(get_env_value(env_var))
        if has_value and is_authorized(required_for, env_var) and not force_refresh:
            configured.append(env_var)
            continue
        need = dict(secret)
        if has_value and force_refresh:
            need["state"] = "refresh"
        else:
            need["state"] = "authorize" if has_value else "missing"
        needs_prompt.append(need)

    response: dict[str, Any] = {}
    if needs_prompt and prompt_callback is not None:
        response = prompt_callback(required_for, needs_prompt) or {}
    values = response.get("values", {}) if isinstance(response.get("values"), dict) else {}
    requested_authorized = response.get("authorized", [])
    if isinstance(requested_authorized, str):
        requested_authorized = [requested_authorized]
    requested_skipped = response.get("skipped", [])
    if isinstance(requested_skipped, str):
        requested_skipped = [requested_skipped]
    requested_skipped = {_normalize_env_var(item) for item in requested_skipped if str(item or "").strip()}
    requested_authorized_set = {
        _normalize_env_var(item)
        for item in requested_authorized
        if str(item or "").strip()
    }

    for need in needs_prompt:
        env_var = need["env_var"]
        if env_var in requested_skipped:
            skipped.append(env_var)
            missing.append(env_var)
            continue
        value = str(values.get(env_var) or "").strip()
        if value:
            save_env_value(env_var, value)
            authorized_now.extend(authorize(required_for, [env_var]))
            configured.append(env_var)
            if need.get("state") == "refresh":
                refreshed.append(env_var)
            continue
        if need.get("state") == "authorize" and env_var in requested_authorized_set:
            authorized_now.extend(authorize(required_for, [env_var]))
            if get_env_value(env_var):
                configured.append(env_var)
            else:
                missing.append(env_var)
            continue
        if prompt_callback is None:
            skipped.append(env_var)
        missing.append(env_var)

    return {
        "success": not missing and not skipped,
        "required_for": required_for,
        "configured": sorted(set(configured)),
        "authorized": sorted(set(authorized_now)),
        "refreshed": sorted(set(refreshed)),
        "skipped": sorted(set(skipped)),
        "missing": sorted(set(missing)),
        "message": "Secret request processed; plaintext values are never returned.",
    }
