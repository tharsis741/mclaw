# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import threading
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate

import httpx
import pytest

from mclaw.skills_hub.catalog import get_text
from mclaw.skills_hub.http_client import MarketplaceClient


@pytest.mark.parametrize("retry_after,delay", [("8", 8), (formatdate(1008, usegmt=True), 8), ("bad", 60)])
def test_origin_cooldown_honors_retry_after_without_blocking_the_other_market(retry_after, delay):
    now, requests, responses = [0.0], [], []
    def respond(request):
        requests.append((str(request.url), now[0]))
        response = httpx.Response(429, headers={"Retry-After": retry_after}) if len(requests) == 1 else httpx.Response(200, text="ready")
        responses.append(response)
        return response
    with MarketplaceClient(transport=httpx.MockTransport(respond), clock=lambda: now[0],
                           wall_clock=lambda: 1000 + now[0]) as client:
        client._wait = lambda delay: now.__setitem__(0, now[0] + delay)
        # Ranking returns promptly; callers can display the saved feed.
        with pytest.raises(httpx.HTTPStatusError):
            get_text("https://www.skills.sh/trending", client=client)
        assert now[0] == 0 and client.retry_at("https://skills.sh").timestamp() == 1000 + delay
        assert get_text("https://api.skillhub.cn/api/v1/skills/office", client=client) == "ready"
        assert now[0] == 0
        assert get_text("https://www.skills.sh/example/repo/skill", client=client) == "ready"
        assert now[0] == delay
        assert get_text("https://www.skills.sh/example/repo/next", client=client) == "ready"
        assert now[0] == delay + 1
        assert all(response.is_closed for response in responses)


def test_detail_retries_once_and_repeated_429_extends_the_shared_cooldown():
    now, requests = [0.0], []
    def respond(request):
        requests.append(now[0])
        return httpx.Response(429)
    with MarketplaceClient(transport=httpx.MockTransport(respond), clock=lambda: now[0],
                           wall_clock=lambda: 1000 + now[0]) as client:
        client._wait = lambda delay: now.__setitem__(0, now[0] + delay)
        with pytest.raises(httpx.HTTPStatusError):
            get_text("https://www.skills.sh/example/repo/skill", client=client)
        assert requests == [0, 60]
        assert client.retry_at("https://www.skills.sh").timestamp() == 1180


def test_skillhub_file_redirects_are_paced_for_both_api_and_content_origins():
    now, requests = [0.0], []
    def respond(request):
        requests.append((request.url.host, now[0]))
        if request.url.host == "api.skillhub.cn":
            return httpx.Response(302, headers={"location": "https://skillhub-1388575217.cos.accelerate.myqcloud.com/skill.md"})
        return httpx.Response(200, text="ready")
    with MarketplaceClient(transport=httpx.MockTransport(respond), clock=lambda: now[0]) as client:
        client._wait = lambda delay: now.__setitem__(0, now[0] + delay)
        for _ in range(2):
            assert get_text("https://api.skillhub.cn/api/v1/skills/office/file", client=client) == "ready"
        assert [at for _host, at in requests] == [0, 0, 0.25, 0.25]


def test_stopping_releases_a_request_waiting_for_cooldown():
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "600"})
    client = MarketplaceClient(transport=httpx.MockTransport(respond))
    waiting = threading.Event()
    def wait(delay):
        waiting.set()
        client._stopping.wait(delay)
    client._wait = wait
    try:
        with pytest.raises(httpx.HTTPStatusError):
            get_text("https://www.skills.sh/trending", client=client)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(get_text, "https://www.skills.sh/example/repo/skill", client=client)
            try:
                assert waiting.wait(timeout=2)
            finally:
                client.stop_waiting()
            with pytest.raises(httpx.RequestError):
                future.result(timeout=2)
        assert len(requests) == 1
    finally:
        client.close()
