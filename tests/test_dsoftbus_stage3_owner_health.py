# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time
from types import MappingProxyType
from typing import Any, Callable, Mapping
import uuid

import pytest

from mclaw.dsoftbus.health import (
    RUNTIME_HEALTH_KEYS,
    RuntimeHealthError,
    RuntimeHealthPublisher,
    parse_runtime_health,
    validate_runtime_health,
    worker_epoch_digest,
)
from mclaw.dsoftbus.owner import DsoftbusOwnerLoopDriver, OwnerLoopError
from mclaw.dsoftbus.runtime import DsoftbusRuntime, DsoftbusRuntimeError, RuntimeState


def _config() -> dict[str, Any]:
    return {
        "dsoftbus": {
            "accept_remote_messages": False,
            "discovery_without_provider": False,
            "enabled": "auto",
            "global_requests_per_minute": 12,
            "per_peer_requests_per_minute": 6,
            "remote_token_budget_per_hour": 100_000,
        }
    }


class _Endpoint:
    def __init__(self, runtime_instance_id: str, order: list[tuple[Any, ...]]) -> None:
        self.holder = MappingProxyType(
            {
                "pid": 123,
                "runtimeInstanceId": runtime_instance_id,
                "schemaVersion": "mclaw.dsoftbus.endpoint-lock/v1",
                "startTimeTicks": 456,
            }
        )
        self.order = order
        self.payloads: list[bytes] = []
        self.release_calls = 0

    def publish_owned_runtime_health(self, raw: bytes, *, temp_name: str) -> None:
        snapshot = parse_runtime_health(raw)
        self.order.append(
            (
                "publish",
                snapshot["state"],
                snapshot["snapshotSequence"],
                threading.get_ident(),
                temp_name,
            )
        )
        self.payloads.append(raw)

    def release(self) -> None:
        self.release_calls += 1
        self.order.append(("release", threading.get_ident()))


class _Resources:
    def __init__(
        self,
        *,
        start_result: Mapping[str, Any] | None = None,
        update_error: BaseException | None = None,
        block_stop: bool = False,
    ) -> None:
        self.start_result = start_result or {
            "degradedReasons": ("PRODUCT_INTEGRATION_UNVERIFIED",),
            "healthUpdates": {},
            "state": "DEGRADED",
        }
        self.update_error = update_error
        self.block_stop = block_stop
        self.calls: list[tuple[Any, ...]] = []
        self.emergency_threads: list[int] = []
        self.callback: Callable[[Mapping[str, Any]], None] | None = None
        self.loop: asyncio.AbstractEventLoop | None = None

    async def start(self, runtime_instance_id: str) -> Mapping[str, Any]:
        self.loop = asyncio.get_running_loop()
        self.calls.append(("start", threading.get_ident(), runtime_instance_id))
        return self.start_result

    def set_health_change_callback(
        self, callback: Callable[[Mapping[str, Any]], None] | None
    ) -> None:
        self.calls.append(("callback", threading.get_ident(), callback is not None))
        self.callback = callback
        if callback is not None:
            callback({"eventOverflowCount": 0})

    def emit_health(self, updates: Mapping[str, Any]) -> None:
        assert self.loop is not None
        completed = threading.Event()
        errors: list[BaseException] = []

        def emit() -> None:
            try:
                assert self.callback is not None
                self.callback(updates)
            except BaseException as error:
                errors.append(error)
            finally:
                completed.set()

        self.loop.call_soon_threadsafe(emit)
        assert completed.wait(2)
        if errors:
            raise errors[0]

    async def begin_shutdown(self) -> Mapping[str, Any] | None:
        self.calls.append(("begin", threading.get_ident()))
        return {}

    async def stop(
        self, deadline: Callable[[], float]
    ) -> Mapping[str, Any] | None:
        self.calls.append(("stop", threading.get_ident(), deadline()))
        if self.block_stop:
            await asyncio.Event().wait()
        return {}

    async def update_provider_runtime(
        self, context: Any | None
    ) -> Mapping[str, Any] | None:
        self.calls.append(("provider", threading.get_ident(), context))
        if self.update_error is not None:
            raise self.update_error
        return {}

    def emergency_reap(self) -> None:
        self.emergency_threads.append(threading.get_ident())


class _SlowStartResources(_Resources):
    async def start(self, runtime_instance_id: str) -> Mapping[str, Any]:
        await asyncio.sleep(0.15)
        return await super().start(runtime_instance_id)


def _runtime(
    resources: _Resources,
    *,
    provider_runtime: Any | None = None,
    readiness: Callable[[Any | None], tuple[bool, str]] | None = None,
) -> tuple[DsoftbusRuntime, DsoftbusOwnerLoopDriver, list[_Endpoint], list[tuple[Any, ...]]]:
    order: list[tuple[Any, ...]] = []
    endpoints: list[_Endpoint] = []

    def endpoint_factory(state_root: Path, runtime_instance_id: str) -> _Endpoint:
        endpoint = _Endpoint(runtime_instance_id, order)
        endpoints.append(endpoint)
        return endpoint

    kwargs: dict[str, Any] = {}
    if readiness is not None:
        kwargs["provider_readiness"] = readiness
    driver = DsoftbusOwnerLoopDriver(
        config=_config(),
        provider_runtime=provider_runtime,
        socket_cap=16,
        resources=resources,
        **kwargs,
    )
    runtime = DsoftbusRuntime(
        provider_runtime=provider_runtime,
        config=_config(),
        workspace=Path.cwd().resolve(),
        state_root=Path.cwd().resolve() / ".test-dsoftbus-state",
        driver=driver,
        endpoint_lock_factory=endpoint_factory,
    )
    return runtime, driver, endpoints, order


def test_resource_start_has_a_separate_outer_budget() -> None:
    resources = _SlowStartResources()
    driver = DsoftbusOwnerLoopDriver(
        config=_config(),
        provider_runtime=None,
        socket_cap=16,
        resources=resources,
        start_timeout=0.1,
        resource_start_timeout=0.5,
    )
    runtime = DsoftbusRuntime(
        provider_runtime=None,
        config=_config(),
        workspace=Path.cwd().resolve(),
        state_root=Path.cwd().resolve() / ".test-dsoftbus-start-budget",
        driver=driver,
        endpoint_lock_factory=lambda _root, instance: _Endpoint(instance, []),
    )

    assert runtime.start()["state"] == RuntimeState.DEGRADED
    runtime.begin_shutdown()
    assert runtime.stop(time.monotonic() + 2)["state"] == RuntimeState.STOPPED


@pytest.mark.parametrize("value", [False, 0, -1, float("inf")])
def test_resource_start_timeout_must_be_finite_and_positive(value: object) -> None:
    error = TypeError if isinstance(value, bool) else ValueError
    with pytest.raises(error):
        DsoftbusOwnerLoopDriver(
            config=_config(),
            provider_runtime=None,
            socket_cap=16,
            resource_start_timeout=value,  # type: ignore[arg-type]
        )


def test_owner_loop_publishes_complete_lifecycle_before_endpoint_release() -> None:
    main_thread = threading.get_ident()
    resources = _Resources()
    runtime, driver, endpoints, order = _runtime(resources)

    started = runtime.start()
    assert started["state"] == RuntimeState.DEGRADED
    health = runtime.health_snapshot()
    assert frozenset(health) == frozenset(RUNTIME_HEALTH_KEYS)
    assert health["state"] == "DEGRADED"
    assert health["degradedReasons"] == ("PRODUCT_INTEGRATION_UNVERIFIED",)
    assert health["providerReadinessCode"] == "PROVIDER_MISSING"
    assert health["snapshotSequence"] == 2
    with pytest.raises(TypeError):
        health["state"] = "READY"  # type: ignore[index]

    runtime.begin_shutdown()
    assert runtime.health_snapshot()["state"] == "STOPPING"
    stopped = runtime.stop(time.monotonic() + 5)
    assert stopped["state"] == RuntimeState.STOPPED
    final_health = driver.health_snapshot()
    assert final_health["state"] == "STOPPED"
    assert final_health["snapshotSequence"] == 4
    assert endpoints[0].release_calls == 1
    assert [item[1] for item in order if item[0] == "publish"] == [
        "STARTING",
        "DEGRADED",
        "STOPPING",
        "STOPPED",
    ]
    assert order[-1][0] == "release"
    publisher_threads = {item[3] for item in order if item[0] == "publish"}
    resource_threads = {item[1] for item in resources.calls}
    assert len(publisher_threads) == 1
    assert publisher_threads == resource_threads
    assert main_thread not in publisher_threads
    assert driver.thread is not None and not driver.thread.is_alive()
    assert parse_runtime_health(endpoints[0].payloads[-1])["state"] == "STOPPED"


def test_resource_health_callback_publishes_only_semantic_changes() -> None:
    resources = _Resources()
    runtime, driver, endpoints, _ = _runtime(resources)

    runtime.start()
    assert driver.health_snapshot()["snapshotSequence"] == 2

    resources.emit_health({"eventOverflowCount": 1})
    changed = driver.health_snapshot()
    assert changed["eventOverflowCount"] == 1
    assert changed["snapshotSequence"] == 3

    resources.emit_health({"eventOverflowCount": 1})
    assert driver.health_snapshot()["snapshotSequence"] == 3

    resources.emit_health({"eventOverflowCount": 2})
    assert driver.health_snapshot()["snapshotSequence"] == 4

    runtime.begin_shutdown()
    assert resources.callback is None
    runtime.stop(time.monotonic() + 5)
    assert [parse_runtime_health(raw)["snapshotSequence"] for raw in endpoints[0].payloads] == [
        1,
        2,
        3,
        4,
        5,
        6,
    ]


def test_resource_callback_can_publish_one_degraded_recovery_transition() -> None:
    resources = _Resources()
    runtime, driver, endpoints, _ = _runtime(resources)
    runtime.start()

    ready = {
        "_degradedReasons": (),
        "_lifecycleState": "READY",
        "listenerReady": True,
        "listenerSocketCount": 1,
        "openSocketCount": 1,
        "productIntegrationVerified": True,
        "workerAlive": True,
        "workerEpoch": "0123456789ab",
        "workerPid": 901,
        "workerStartTimeTicks": 902,
    }
    resources.emit_health(ready)
    assert driver.health_snapshot()["state"] == "READY"

    resources.emit_health(
        {
            "_degradedReasons": ("WORKER_START_FAILED",),
            "_lifecycleState": "DEGRADED",
            "listenerReady": False,
            "listenerSocketCount": 0,
            "openSocketCount": 0,
            "workerAlive": False,
            "workerEpoch": None,
            "workerPid": None,
            "workerStartTimeTicks": None,
        }
    )
    degraded = driver.health_snapshot()
    assert degraded["state"] == "DEGRADED"
    assert degraded["degradedReasons"] == ("WORKER_START_FAILED",)

    resources.emit_health(
        {
            **ready,
            "restartCount": 1,
            "workerEpoch": "fedcba987654",
            "workerPid": 903,
            "workerStartTimeTicks": 904,
        }
    )
    recovered = driver.health_snapshot()
    assert recovered["state"] == "READY"
    assert recovered["degradedReasons"] == ()
    assert recovered["restartCount"] == 1
    assert recovered["workerPid"] == 903

    runtime.stop(time.monotonic() + 5)
    assert [
        parse_runtime_health(raw)["state"] for raw in endpoints[0].payloads
    ] == ["STARTING", "DEGRADED", "READY", "DEGRADED", "READY", "STOPPING", "STOPPED"]


def test_provider_updates_are_marshaled_and_health_uses_explicit_readiness() -> None:
    resources = _Resources()
    context = object()
    runtime, driver, _, _ = _runtime(
        resources,
        readiness=lambda value: (True, "") if value is context else (False, "PROVIDER_MISSING"),
    )
    runtime.start()
    owner_thread = driver.owner_thread_id

    runtime.update_provider_runtime(context)
    ready = runtime.health_snapshot()
    assert ready["state"] == "DEGRADED"
    assert ready["providerReady"] is True
    assert ready["providerReadinessCode"] == ""
    runtime.update_provider_runtime(None)
    missing = runtime.health_snapshot()
    assert missing["providerReady"] is False
    assert missing["providerReadinessCode"] == "PROVIDER_MISSING"
    assert all(
        call[1] == owner_thread
        for call in resources.calls
        if call[0] == "provider"
    )
    runtime.stop(time.monotonic() + 5)


def test_provider_sync_failure_is_persisted_and_runtime_context_rolls_back() -> None:
    resources = _Resources(update_error=RuntimeError("sync failed"))
    old_context = object()
    new_context = object()
    runtime, _, _, _ = _runtime(
        resources,
        provider_runtime=old_context,
        readiness=lambda value: (True, "") if value is not None else (False, "PROVIDER_MISSING"),
    )
    runtime.start()
    with pytest.raises(RuntimeError, match="sync failed"):
        runtime.update_provider_runtime(new_context)
    health = runtime.health_snapshot()
    assert health["providerReady"] is False
    assert health["providerReadinessCode"] == "PROVIDER_SYNC_FAILED"
    resources.update_error = None
    runtime.update_provider_runtime(old_context)
    assert runtime.health_snapshot()["providerReady"] is True
    runtime.stop(time.monotonic() + 5)


def test_invalid_resource_outcome_rolls_back_on_owner_and_never_publishes_ready() -> None:
    resources = _Resources(
        start_result={
            "degradedReasons": (),
            "healthUpdates": {},
            "state": "DEGRADED",
        }
    )
    runtime, driver, endpoints, order = _runtime(resources)
    with pytest.raises(OwnerLoopError) as caught:
        runtime.start()
    assert caught.value.code == "OWNER_RESOURCE_RESULT_INVALID"
    assert runtime.state == RuntimeState.NEW
    assert endpoints[0].release_calls == 1
    assert "READY" not in [item[1] for item in order if item[0] == "publish"]
    assert [item[1] for item in order if item[0] == "publish"] == [
        "STARTING",
        "DEGRADED",
        "STOPPING",
        "STOPPED",
    ]
    assert driver.thread is not None and not driver.thread.is_alive()


def test_deadline_emergency_reap_does_not_publish_host_synthesized_stopped() -> None:
    main_thread = threading.get_ident()
    resources = _Resources(block_stop=True)
    runtime, driver, endpoints, order = _runtime(resources)
    runtime.start()
    with pytest.raises(DsoftbusRuntimeError) as caught:
        runtime.stop(time.monotonic() + 0.10)
    assert caught.value.code == "RUNTIME_STOP_FAILED"
    assert resources.emergency_threads == [main_thread]
    assert endpoints[0].release_calls == 1
    assert "STOPPED" not in [item[1] for item in order if item[0] == "publish"]
    assert order[-1][0] == "release"
    assert driver.thread is not None
    driver.thread.join(2)
    assert not driver.thread.is_alive()


def test_health_validation_rejects_impossible_and_noncanonical_snapshots() -> None:
    resources = _Resources()
    runtime, _, endpoints, _ = _runtime(resources)
    runtime.start()
    valid = json.loads(endpoints[0].payloads[-1])
    validate_runtime_health(valid)

    bad_bool = dict(valid)
    bad_bool["workerAlive"] = 0
    with pytest.raises(RuntimeHealthError, match="RUNTIME_HEALTH_INVALID"):
        validate_runtime_health(bad_bool)

    bad_ready = dict(valid)
    bad_ready.update(
        {
            "degradedReasons": [],
            "primaryErrorCode": "",
            "state": "READY",
        }
    )
    with pytest.raises(RuntimeHealthError, match="RUNTIME_HEALTH_INVALID"):
        validate_runtime_health(bad_ready)

    bad_epoch_pair = dict(valid)
    bad_epoch_pair["workerEpoch"] = "0123456789ab"
    with pytest.raises(RuntimeHealthError, match="RUNTIME_HEALTH_INVALID"):
        validate_runtime_health(bad_epoch_pair)

    noncanonical = json.dumps(valid, indent=2).encode("utf-8")
    with pytest.raises(RuntimeHealthError, match="RUNTIME_HEALTH_INVALID"):
        parse_runtime_health(noncanonical)
    runtime.stop(time.monotonic() + 5)


def test_health_publisher_rejects_host_thread_and_epoch_digest_is_stable() -> None:
    endpoint = _Endpoint(str(uuid.uuid4()), [])
    with pytest.raises(RuntimeHealthError) as caught:
        RuntimeHealthPublisher(
            endpoint=endpoint,  # type: ignore[arg-type]
            runtime_instance_id=endpoint.holder["runtimeInstanceId"],
            config=_config(),
            socket_cap=16,
            provider_ready=False,
            provider_readiness_code="PROVIDER_MISSING",
            now=lambda: datetime(2026, 8, 11, tzinfo=timezone.utc),
        )
    assert caught.value.code == "HEALTH_OWNER_THREAD_REQUIRED"

    private = uuid.UUID("123e4567-e89b-42d3-a456-426614174000")
    assert worker_epoch_digest(private) == worker_epoch_digest(str(private))
    assert len(worker_epoch_digest(private)) == 12


def test_owner_loop_public_facade_rejects_owner_reentrancy() -> None:
    class ReentrantResources(_Resources):
        driver: DsoftbusOwnerLoopDriver

        async def start(self, runtime_instance_id: str) -> Mapping[str, Any]:
            self.driver.update_provider_runtime(None)
            raise AssertionError("reentrant call unexpectedly returned")

    resources = ReentrantResources()
    runtime, driver, endpoints, _ = _runtime(resources)
    resources.driver = driver
    with pytest.raises(OwnerLoopError) as caught:
        runtime.start()
    assert caught.value.code == "OWNER_LOOP_REENTRANCY"
    assert endpoints[0].release_calls == 1
    assert driver.thread is not None and not driver.thread.is_alive()
