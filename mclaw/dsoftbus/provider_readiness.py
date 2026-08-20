# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Fail-closed Provider readiness shared by the Runtime owner and resources."""

from __future__ import annotations

from typing import Any

from mclaw.agent.transports.factory import resolve_transport_class
from mclaw.providers.runtime import ProviderRuntimeContext


def resolve_provider_readiness(context: Any | None) -> tuple[bool, str]:
    """Return the public readiness pair without constructing an SDK client."""

    if context is None:
        return False, "PROVIDER_MISSING"
    if not isinstance(context, ProviderRuntimeContext):
        return False, "PROVIDER_MISSING"
    try:
        transport_class = resolve_transport_class(context)
    except (TypeError, ValueError):
        return False, "PROVIDER_MISSING"
    if not bool(
        getattr(transport_class, "supports_dsoftbus_remote_fence", False)
    ):
        return False, "TRANSPORT_FENCE_UNSUPPORTED"
    return True, ""


__all__ = ["resolve_provider_readiness"]
