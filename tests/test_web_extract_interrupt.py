from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from mclaw.tools import dispatch, web_extract_tool
from mclaw.tools.interrupt import reset_interrupt_event, set_interrupt_event


def _parent(backend: str) -> SimpleNamespace:
    return SimpleNamespace(
        config={
            "auxiliary": {
                "web_extract": {
                    "backend": backend,
                    "firecrawl_api_url": "https://firecrawl.test/v2/scrape",
                }
            }
        }
    )


def test_web_extract_pre_cancel_does_not_enter_configuration_or_provider(monkeypatch) -> None:
    cancel_event = threading.Event()
    cancel_event.set()
    configured = []
    monkeypatch.setattr(web_extract_tool, "_config", lambda **_kwargs: configured.append(True))

    token = set_interrupt_event(cancel_event)
    try:
        result = json.loads(web_extract_tool.web_extract(["https://example.com/article"]))
    finally:
        reset_interrupt_event(token)

    assert configured == []
    assert result == {
        "error": "Web extraction interrupted by user",
        "success": False,
        "interrupted": True,
        "status": "cancelled",
    }


def test_tavily_cancel_after_http_return_skips_response_processing(monkeypatch) -> None:
    cancel_event = threading.Event()
    response_touched = []

    async def fake_post(*_args, **_kwargs):
        cancel_event.set()
        return SimpleNamespace(status_code=200, headers={}), {"results": []}

    monkeypatch.setattr(web_extract_tool, "_authorized_env_value", lambda _name: "secret")
    monkeypatch.setattr(web_extract_tool, "_is_safe_url", lambda _url: True)
    monkeypatch.setattr(web_extract_tool, "_post_json_async", fake_post)

    token = set_interrupt_event(cancel_event)
    try:
        result = json.loads(
            web_extract_tool.web_extract(
                ["https://example.com/article"],
                parent_agent=_parent("tavily"),
            )
        )
    finally:
        reset_interrupt_event(token)

    assert response_touched == []
    assert result["interrupted"] is True
    assert result["status"] == "cancelled"


def test_firecrawl_cancel_after_first_http_return_does_not_start_next_url(monkeypatch) -> None:
    cancel_event = threading.Event()
    requested_urls = []

    # Set the event while returning from HTTP without relying on response methods.
    async def returning_post(*_args, **kwargs):
        requested_urls.append(kwargs["payload"]["url"])
        cancel_event.set()
        return SimpleNamespace(status_code=200, headers={}), {}

    monkeypatch.setattr(web_extract_tool, "_authorized_env_value", lambda _name: "secret")
    monkeypatch.setattr(web_extract_tool, "_is_safe_url", lambda _url: True)
    monkeypatch.setattr(web_extract_tool, "_post_json_async", returning_post)

    token = set_interrupt_event(cancel_event)
    try:
        result = json.loads(
            web_extract_tool.web_extract(
                ["https://example.com/one", "https://example.com/two"],
                parent_agent=_parent("firecrawl"),
            )
        )
    finally:
        reset_interrupt_event(token)

    assert requested_urls == ["https://example.com/one"]
    assert result["interrupted"] is True
    assert result["status"] == "cancelled"


def test_public_page_dns_validation_does_not_block_shared_loop(monkeypatch) -> None:
    validation_started = threading.Event()
    loop_progressed = threading.Event()
    release_validation = threading.Event()
    progress_observed = []

    class DummyClient:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            pass

    def slow_url_error(_url: str) -> str:
        validation_started.set()
        release_validation.wait(1)
        return "blocked before network"

    monkeypatch.setattr(web_extract_tool.httpx, "AsyncClient", DummyClient)
    monkeypatch.setattr(web_extract_tool, "_url_error", slow_url_error)

    def observe_progress() -> None:
        if not validation_started.wait(1):
            progress_observed.append(None)
        else:
            progress_observed.append(loop_progressed.wait(0.25))
        release_validation.set()

    observer = threading.Thread(target=observe_progress)
    observer.start()

    async def probe() -> None:
        fetch = asyncio.create_task(
            web_extract_tool._fetch_public_page_async("https://example.test", 1)
        )
        while not validation_started.is_set():
            await asyncio.sleep(0.001)
        loop_progressed.set()
        with pytest.raises(ValueError, match="blocked before network"):
            await fetch

    asyncio.run(probe())
    observer.join(1)
    assert progress_observed == [True]


@pytest.mark.parametrize("phase", ["initial", "firecrawl_final"])
def test_production_url_dns_validation_is_cancellable_off_loop(phase, monkeypatch) -> None:
    initial_url = "https://initial.example/article"
    final_url = "https://final.example/result"
    validation_started = threading.Event()
    validation_finished = threading.Event()
    release_validation = threading.Event()
    finished = threading.Event()
    cancel_event = threading.Event()
    validator_threads = []
    caller_threads = []
    results = []

    def slow_url_error(url: str) -> str:
        should_block = (
            phase == "initial" and url == initial_url
        ) or (
            phase == "firecrawl_final" and url == final_url
        )
        if should_block:
            validator_threads.append(threading.get_ident())
            validation_started.set()
            release_validation.wait(2)
            validation_finished.set()
        return ""

    async def fake_post(*_args, **_kwargs):
        return SimpleNamespace(status_code=200, headers={}), {
            "success": True,
            "data": {
                "markdown": "content",
                "metadata": {"sourceURL": final_url},
            },
        }

    monkeypatch.setattr(web_extract_tool, "_url_error", slow_url_error)
    monkeypatch.setattr(web_extract_tool, "_post_json_async", fake_post)
    monkeypatch.setattr(web_extract_tool, "_authorized_env_value", lambda _name: "test")

    def run() -> None:
        caller_threads.append(threading.get_ident())
        token = set_interrupt_event(cancel_event)
        try:
            results.append(json.loads(web_extract_tool.web_extract(
                [initial_url],
                parent_agent=_parent("firecrawl"),
            )))
        finally:
            reset_interrupt_event(token)
            finished.set()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    try:
        assert validation_started.wait(1)
        assert dispatch._run_async(
            asyncio.sleep(0, result="loop alive"),
            diagnostic_name="url_validation_heartbeat",
            timeout_seconds=0.5,
            raise_on_stop=True,
        ) == "loop alive"
        cancel_event.set()
        assert finished.wait(1)
    finally:
        release_validation.set()
        worker.join(1)

    assert not worker.is_alive()
    assert validation_finished.wait(1)
    assert validator_threads and validator_threads[0] != caller_threads[0]
    assert results[0]["status"] == "cancelled"
