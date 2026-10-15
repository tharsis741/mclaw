# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Pooled public reads with per-origin pacing and shared rate-limit cooldowns."""

from __future__ import annotations

import math
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import httpx


class MarketplaceClient(httpx.Client):
    """Space requests and retry a 429 once after the origin's cooldown."""

    def __init__(self, *, clock=None, wall_clock=None, **kwargs):
        self._clock = clock or time.monotonic
        self._wall_clock = wall_clock or time.time
        self._request_guard = threading.Lock()
        self._stopping = threading.Event()
        self._wait = self._stopping.wait
        self._origins: dict[str, dict] = {}
        super().__init__(limits=httpx.Limits(max_connections=8, max_keepalive_connections=8,
                                             keepalive_expiry=30),
                         event_hooks={"request": [self._before_request], "response": [self._after_response]},
                         **kwargs)

    @staticmethod
    def _origin(url) -> str:
        return (urlparse(str(url)).hostname or "").lower().removeprefix("www.")

    def _origin_state(self, origin: str) -> dict:
        return self._origins.setdefault(origin, {"next": 0.0, "blocked": 0.0, "failures": 0})

    def _before_request(self, request: httpx.Request) -> None:
        origin = self._origin(request.url)
        interval = 0.25 if origin in {"api.skillhub.cn", "skillhub-1388575217.cos.accelerate.myqcloud.com"} else 1.0
        while not self._stopping.is_set():
            with self._request_guard:
                state = self._origin_state(origin)
                now = self._clock()
                delay = max(state["next"], state["blocked"]) - now
                if delay <= 0:
                    state["next"] = now + interval
                    return
            self._wait(min(delay, 60.0))
        raise httpx.RequestError("技能市场请求已停止", request=request)

    def _after_response(self, response: httpx.Response) -> None:
        with self._request_guard:
            state = self._origin_state(self._origin(response.url))
            now = self._clock()
            if response.status_code != 429:
                if response.is_success and state["blocked"] <= now:
                    state["failures"] = 0
                return
            state["failures"] = min(state["failures"] + 1, 5)
            delay = min(60 * 2 ** (state["failures"] - 1), 900)
            header = response.headers.get("Retry-After", "")
            try:
                try:
                    requested = float(header)
                except ValueError:
                    at = parsedate_to_datetime(header)
                    requested = at.replace(tzinfo=at.tzinfo or timezone.utc).timestamp() - self._wall_clock()
                if math.isfinite(requested):
                    delay = max(0.0, requested)
            except (ValueError, TypeError, OverflowError):
                pass
            state["blocked"] = max(state["blocked"], now + delay)

    def retry_at(self, url: str) -> datetime | None:
        with self._request_guard:
            delay = self._origin_state(self._origin(url))["blocked"] - self._clock()
        if delay <= 0:
            return None
        try:
            return datetime.fromtimestamp(self._wall_clock() + delay, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return datetime.max.replace(tzinfo=timezone.utc)

    @contextmanager
    def stream(self, *args, **kwargs):
        url = kwargs.get("url") or args[1]
        # A manual leaderboard refresh returns its cached list promptly on rate limiting.
        attempts = 1 if urlparse(str(url)).path in {"/trending", "/api/v1/showcase/trending"} else 2
        for attempt in range(attempts):
            with super().stream(*args, **kwargs) as response:
                if response.status_code != 429 or attempt == attempts - 1:
                    yield response
                    return
            # The next request hook waits; the failed response is already closed.

    def stop_waiting(self) -> None:
        self._stopping.set()

    def close(self) -> None:
        self.stop_waiting()
        super().close()
