# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Retry utilities — jittered backoff for decorrelated retries."""

import random
import threading
import time

_jitter_counter = 0
_jitter_lock = threading.Lock()


def jittered_backoff(
    attempt: int,
    *,
    base_delay: float = 5.0,
    max_delay: float = 120.0,
    jitter_ratio: float = 0.5,
) -> float:
    global _jitter_counter
    with _jitter_lock:
        _jitter_counter += 1
        tick = _jitter_counter

    exponent = max(0, attempt - 1)
    if exponent >= 63 or base_delay <= 0:
        delay = max_delay
    else:
        delay = min(base_delay * (2 ** exponent), max_delay)

    seed = (time.time_ns() ^ (tick * 0x9E3779B9)) & 0xFFFFFFFF
    rng = random.Random(seed)
    jitter = rng.uniform(0, jitter_ratio * delay)
    return delay + jitter


RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 529}


def is_retryable_error(exc: Exception) -> bool:
    import openai
    import anthropic
    if isinstance(exc, (openai.RateLimitError, anthropic.RateLimitError)):
        return True
    if isinstance(exc, (openai.APIStatusError, anthropic.APIStatusError)):
        return getattr(exc, "status_code", 0) in RETRYABLE_STATUS_CODES
    if isinstance(exc, (openai.APIConnectionError, anthropic.APIConnectionError)):
        return True
    return False


def get_retry_after(exc: Exception) -> float | None:
    """Extract Retry-After header value from API error if present."""
    headers = getattr(getattr(exc, "response", None), "headers", {})
    val = headers.get("retry-after") or headers.get("Retry-After")
    if val:
        try:
            return float(val)
        except (ValueError, TypeError):
            pass
    return None
