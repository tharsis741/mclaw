# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Live, secret-bearing provider runtime context."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping
from urllib.parse import urlsplit, urlunsplit

if TYPE_CHECKING:
    from mclaw.providers.base import RuntimeProviderProfile


_SNAPSHOT_SCHEMA_VERSION = 1


def _fingerprint(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}" if value else ""


def _stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class ProviderRuntimeContext:
    """The single immutable representation of live provider state."""

    profile: RuntimeProviderProfile
    model: str
    api_key: str
    base_url: str
    base_url_source: str = "profile"
    auth_source: str = ""
    reasoning_config: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.reasoning_config is not None:
            copied = deepcopy(dict(self.reasoning_config))
            object.__setattr__(self, "reasoning_config", MappingProxyType(copied))

    @property
    def provider(self) -> str:
        return self.profile.name

    @property
    def api_mode(self) -> str:
        return self.profile.api_mode

    @property
    def api_key_fingerprint(self) -> str:
        return _fingerprint(self.api_key)

    @property
    def safe_base_url(self) -> str:
        """Return only the endpoint scheme, host, port, and normalized path."""
        if not self.base_url:
            return ""
        parsed = urlsplit(self.base_url)
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            port = f":{parsed.port}" if parsed.port is not None else ""
        except ValueError:
            port = ""
        path = parsed.path.rstrip("/")
        return urlunsplit((parsed.scheme.lower(), f"{host.lower()}{port}", path, "", ""))

    def snapshot(self) -> dict[str, Any]:
        """Return the secret-free, schema-versioned persistence allowlist."""
        reasoning_config = (
            deepcopy(dict(self.reasoning_config))
            if self.reasoning_config is not None
            else None
        )
        return {
            "schema_version": _SNAPSHOT_SCHEMA_VERSION,
            "provider": self.provider,
            "model": self.model,
            "safe_base_url": self.safe_base_url,
            "base_url_source": self.base_url_source,
            "api_mode": self.api_mode,
            "auth_source": self.auth_source,
            "api_key_fingerprint": self.api_key_fingerprint,
            "reasoning_config": reasoning_config,
        }

    def fingerprint(self) -> tuple[str, ...]:
        """Identify state that changes client construction or request behavior."""
        reasoning_config = (
            dict(self.reasoning_config) if self.reasoning_config is not None else None
        )
        return (
            self.provider,
            self.model,
            self.api_mode,
            self.profile.auth_scheme,
            _fingerprint(self.safe_base_url),
            self.api_key_fingerprint,
            _stable_json(reasoning_config),
        )
