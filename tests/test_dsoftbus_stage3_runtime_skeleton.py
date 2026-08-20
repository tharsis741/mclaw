# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time
from typing import Any
import uuid

import pytest

from mclaw.dsoftbus.active import (
    ActiveRuntimeError,
    clear_active_runtime,
    get_active_runtime,
    install_active_runtime,
)
from mclaw.dsoftbus.endpoint_lock import (
    DsoftbusEndpointLock,
    ENDPOINT_LOCK_SCHEMA,
    EndpointLockError,
    build_endpoint_holder,
    parse_endpoint_holder,
    read_process_start_time_ticks,
)
from mclaw.dsoftbus.protocol import canonical_json_bytes
from mclaw.dsoftbus.runtime import (
    DsoftbusRuntime,
    DsoftbusRuntimeError,
    RuntimeState,
)


@pytest.fixture(autouse=True)
def _clear_active_after_test() -> None:
    yield
    current = get_active_runtime()
    if current is not None:
        assert clear_active_runtime(current)


class _FakeEndpointLock:
    def __init__(self, *, fail_release: bool = False) -> None:
        self.fail_release = fail_release
        self.release_calls = 0

    def release(self) -> None:
        self.release_calls += 1
        if self.fail_release:
            raise EndpointLockError("ENDPOINT_LOCK_OWNERSHIP_LOST")


class _FakeEndpointFactory:
    def __init__(self, lock: _FakeEndpointLock | None = None) -> None:
        self.lock = lock or _FakeEndpointLock()
        self.calls: list[tuple[Path, str]] = []

    def __call__(self, state_root: Path, runtime_instance_id: str) -> _FakeEndpointLock:
        self.calls.append((state_root, runtime_instance_id))
        return self.lock


class _BlockingEndpointFactory(_FakeEndpointFactory):
    def __init__(
        self,
        *,
        entered: threading.Event,
        release: threading.Event,
        error: EndpointLockError | None = None,
    ) -> None:
        super().__init__()
        self.entered = entered
        self.release = release
        self.error = error

    def __call__(self, state_root: Path, runtime_instance_id: str) -> _FakeEndpointLock:
        self.calls.append((state_root, runtime_instance_id))
        self.entered.set()
        assert self.release.wait(5)
        if self.error is not None:
            raise self.error
        return self.lock


class _FakeDriver:
    def __init__(
        self,
        *,
        state: str = RuntimeState.DEGRADED,
        reasons: tuple[str, ...] = ("PRODUCT_INTEGRATION_UNVERIFIED",),
        start_entered: threading.Event | None = None,
        start_release: threading.Event | None = None,
        stop_entered: threading.Event | None = None,
        stop_release: threading.Event | None = None,
    ) -> None:
        self.state = state
        self.reasons = reasons
        self.start_entered = start_entered
        self.start_release = start_release
        self.stop_entered = stop_entered
        self.stop_release = stop_release
        self.start_calls: list[str] = []
        self.begin_calls = 0
        self.stop_calls: list[Any] = []
        self.stop_observed_deadlines: list[float] = []
        self.provider_updates: list[Any | None] = []
        self.provider_error: BaseException | None = None
        self.trusted_devices = [
            {
                "deviceIdSha256": "d" * 64,
                "deviceName": "Kaihong B",
                "deviceTypeId": 533,
                "online": True,
                "publicDeviceId": "urn:mclaw:device:oh:" + "b" * 64,
            }
        ]
        self.discovered_devices = [
            {
                "deviceIdSha256": "e" * 64,
                "deviceName": "Kaihong C",
                "deviceTypeId": 533,
            }
        ]
        self.pair_calls: list[str] = []
        self.unbind_calls: list[str] = []

    def start(
        self, runtime_instance_id: str, endpoint_lock: Any
    ) -> dict[str, Any]:
        self.start_calls.append(runtime_instance_id)
        if self.start_entered is not None:
            self.start_entered.set()
        if self.start_release is not None:
            assert self.start_release.wait(5)
        return {"degradedReasons": self.reasons, "state": self.state}

    def begin_shutdown(self) -> None:
        self.begin_calls += 1

    def stop(self, deadline: Any) -> None:
        self.stop_calls.append(deadline)
        if self.stop_entered is not None:
            self.stop_entered.set()
        if self.stop_release is not None:
            assert self.stop_release.wait(5)
        self.stop_observed_deadlines.append(deadline())

    def update_provider_runtime(self, context: Any | None) -> None:
        self.provider_updates.append(context)
        if self.provider_error is not None:
            raise self.provider_error

    def list_trusted_devices(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(value) for value in self.trusted_devices)

    def discover_devices(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(value) for value in self.discovered_devices)

    def pair_device(self, device_id_sha256: str) -> dict[str, Any]:
        self.pair_calls.append(device_id_sha256)
        return {
            "bound": True,
            "deviceIdSha256": device_id_sha256,
            "nativeCode": 0,
            "status": "bound",
        }

    def unbind_device(self, device_id_sha256: str) -> dict[str, Any]:
        self.unbind_calls.append(device_id_sha256)
        self.trusted_devices = [
            value
            for value in self.trusted_devices
            if value["deviceIdSha256"] != device_id_sha256
        ]
        return {
            "deviceIdSha256": device_id_sha256,
            "publicDeviceId": "urn:mclaw:device:oh:" + "b" * 64,
            "unbound": True,
        }


def _runtime(
    tmp_path: Path,
    *,
    driver: _FakeDriver | None = None,
    endpoint_factory: _FakeEndpointFactory | None = None,
    provider_runtime: Any | None = None,
) -> tuple[DsoftbusRuntime, _FakeDriver, _FakeEndpointFactory]:
    selected_driver = driver or _FakeDriver()
    selected_factory = endpoint_factory or _FakeEndpointFactory()
    runtime = DsoftbusRuntime(
        provider_runtime=provider_runtime,
        config={"dsoftbus": {"enabled": "auto"}},
        workspace=tmp_path,
        state_root=tmp_path / "home" / "dsoftbus",
        driver=selected_driver,
        endpoint_lock_factory=selected_factory,
    )
    return runtime, selected_driver, selected_factory


def test_active_accessor_is_identity_idempotent_and_compare_clears() -> None:
    first = object()
    second = object()
    assert get_active_runtime() is None
    install_active_runtime(first)
    install_active_runtime(first)
    assert get_active_runtime() is first
    with pytest.raises(ActiveRuntimeError) as caught:
        install_active_runtime(second)
    assert caught.value.code == "ACTIVE_RUNTIME_EXISTS"
    assert clear_active_runtime(second) is False
    assert get_active_runtime() is first
    assert clear_active_runtime(first) is True
    assert get_active_runtime() is None


def test_active_accessor_serializes_competing_installers() -> None:
    barrier = threading.Barrier(3)
    values = [object(), object()]
    outcomes: list[str] = []

    def install(value: object) -> None:
        barrier.wait()
        try:
            install_active_runtime(value)
            outcomes.append("installed")
        except ActiveRuntimeError as error:
            outcomes.append(error.code)

    threads = [threading.Thread(target=install, args=(value,)) for value in values]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
    assert sorted(outcomes) == ["ACTIVE_RUNTIME_EXISTS", "installed"]
    assert get_active_runtime() in values


def test_constructor_has_no_endpoint_or_driver_start_io(tmp_path: Path) -> None:
    runtime, driver, endpoint_factory = _runtime(tmp_path)
    assert runtime.state == RuntimeState.NEW
    assert endpoint_factory.calls == []
    assert driver.start_calls == []
    snapshot = runtime.health_snapshot()
    assert snapshot["state"] == RuntimeState.NEW
    assert snapshot["providerReady"] is False
    with pytest.raises(TypeError):
        snapshot["state"] = RuntimeState.READY  # type: ignore[index]


def test_start_reuses_one_resource_set_and_stop_compare_clears(tmp_path: Path) -> None:
    runtime, driver, endpoint_factory = _runtime(tmp_path)
    install_active_runtime(runtime)
    first = runtime.start()
    second = runtime.start()
    assert first["state"] == RuntimeState.DEGRADED
    assert first["degradedReasons"] == ("PRODUCT_INTEGRATION_UNVERIFIED",)
    assert second == first
    assert len(endpoint_factory.calls) == 1
    assert driver.start_calls == [runtime.runtime_instance_id]
    stopped = runtime.stop(time.monotonic() + 5)
    assert stopped["state"] == RuntimeState.STOPPED
    assert driver.begin_calls == 1
    assert len(driver.stop_calls) == 1
    assert endpoint_factory.lock.release_calls == 1
    assert get_active_runtime() is None
    again = runtime.stop(time.monotonic() + 10)
    assert again["state"] == RuntimeState.STOPPED
    assert len(driver.stop_calls) == 1
    assert endpoint_factory.lock.release_calls == 1


def test_runtime_lists_redacted_trust_targets_and_unbinds_one_exact_digest(
    tmp_path: Path,
) -> None:
    driver = _FakeDriver(state=RuntimeState.READY, reasons=())
    runtime, _, _ = _runtime(tmp_path, driver=driver)
    runtime.start()

    listed = runtime.list_trusted_devices()
    assert listed == driver.trusted_devices
    assert "networkId" not in str(listed)
    listed[0]["deviceName"] = "local mutation"
    assert driver.trusted_devices[0]["deviceName"] == "Kaihong B"

    with pytest.raises(DsoftbusRuntimeError) as invalid:
        runtime.unbind_device("not-a-digest")
    assert invalid.value.code == "INVALID_PARAMS"
    assert driver.unbind_calls == []

    result = runtime.unbind_device("d" * 64)
    assert result == {
        "deviceIdSha256": "d" * 64,
        "publicDeviceId": "urn:mclaw:device:oh:" + "b" * 64,
        "unbound": True,
    }
    assert driver.unbind_calls == ["d" * 64]
    assert runtime.list_trusted_devices() == []
    runtime.stop(time.monotonic() + 5)


def test_runtime_discovers_redacted_candidates_and_binds_one_exact_digest(
    tmp_path: Path,
) -> None:
    driver = _FakeDriver(state=RuntimeState.READY, reasons=())
    runtime, _, _ = _runtime(tmp_path, driver=driver)
    runtime.start()

    discovered = runtime.discover_devices()
    assert discovered == driver.discovered_devices
    assert "raw" not in repr(discovered)
    discovered[0]["deviceName"] = "local mutation"
    assert driver.discovered_devices[0]["deviceName"] == "Kaihong C"

    with pytest.raises(DsoftbusRuntimeError) as invalid:
        runtime.pair_device("not-a-digest")
    assert invalid.value.code == "INVALID_PARAMS"
    assert driver.pair_calls == []

    result = runtime.pair_device("e" * 64)
    assert result == {
        "bound": True,
        "deviceIdSha256": "e" * 64,
        "nativeCode": 0,
        "status": "bound",
    }
    assert driver.pair_calls == ["e" * 64]
    runtime.stop(time.monotonic() + 5)


def test_begin_shutdown_from_new_is_terminal_and_stop_clears_active(tmp_path: Path) -> None:
    runtime, driver, endpoint_factory = _runtime(tmp_path)
    install_active_runtime(runtime)
    runtime.begin_shutdown()
    assert runtime.state == RuntimeState.STOPPED
    runtime.stop(time.monotonic() + 5)
    assert get_active_runtime() is None
    assert driver.start_calls == []
    assert endpoint_factory.calls == []


def test_concurrent_start_calls_share_one_driver_start(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()
    driver = _FakeDriver(start_entered=entered, start_release=release)
    runtime, _, endpoint_factory = _runtime(tmp_path, driver=driver)
    results: list[dict[str, Any]] = []

    def start() -> None:
        results.append(dict(runtime.start()))

    first = threading.Thread(target=start)
    second = threading.Thread(target=start)
    first.start()
    assert entered.wait(5)
    second.start()
    time.sleep(0.02)
    assert len(driver.start_calls) == 1
    release.set()
    first.join(5)
    second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert len(results) == 2
    assert results[0] == results[1]
    assert len(endpoint_factory.calls) == 1
    runtime.stop(time.monotonic() + 5)


def test_stopping_wins_over_late_start_publication(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()
    driver = _FakeDriver(start_entered=entered, start_release=release)
    runtime, _, endpoint_factory = _runtime(tmp_path, driver=driver)
    start_results: list[dict[str, Any]] = []
    stop_results: list[dict[str, Any]] = []

    start_thread = threading.Thread(
        target=lambda: start_results.append(dict(runtime.start()))
    )
    stop_thread = threading.Thread(
        target=lambda: stop_results.append(
            dict(runtime.stop(time.monotonic() + 5))
        )
    )
    start_thread.start()
    assert entered.wait(5)
    stop_thread.start()
    deadline = time.monotonic() + 2
    while runtime.state != RuntimeState.STOPPING and time.monotonic() < deadline:
        time.sleep(0.001)
    assert runtime.state == RuntimeState.STOPPING
    release.set()
    start_thread.join(5)
    stop_thread.join(5)
    assert not start_thread.is_alive() and not stop_thread.is_alive()
    assert start_results[0]["state"] == RuntimeState.STOPPING
    assert stop_results[0]["state"] == RuntimeState.STOPPED
    assert driver.begin_calls == 1
    assert len(driver.stop_calls) == 1
    assert endpoint_factory.lock.release_calls == 1


def test_stop_during_endpoint_acquire_prevents_driver_start(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()
    endpoint_factory = _BlockingEndpointFactory(entered=entered, release=release)
    driver = _FakeDriver()
    runtime, _, _ = _runtime(
        tmp_path,
        driver=driver,
        endpoint_factory=endpoint_factory,
    )
    start_results: list[dict[str, Any]] = []
    stop_results: list[dict[str, Any]] = []
    starter = threading.Thread(
        target=lambda: start_results.append(dict(runtime.start()))
    )
    stopper = threading.Thread(
        target=lambda: stop_results.append(
            dict(runtime.stop(time.monotonic() + 5))
        )
    )
    starter.start()
    assert entered.wait(5)
    stopper.start()
    deadline = time.monotonic() + 2
    while runtime.state != RuntimeState.STOPPING and time.monotonic() < deadline:
        time.sleep(0.001)
    release.set()
    starter.join(5)
    stopper.join(5)
    assert not starter.is_alive() and not stopper.is_alive()
    assert start_results[0]["state"] == RuntimeState.STOPPING
    assert stop_results[0]["state"] == RuntimeState.STOPPED
    assert driver.start_calls == []
    assert endpoint_factory.lock.release_calls == 1


def test_endpoint_failure_racing_stop_does_not_reopen_runtime(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()
    endpoint_factory = _BlockingEndpointFactory(
        entered=entered,
        release=release,
        error=EndpointLockError("ENDPOINT_IN_USE"),
    )
    runtime, driver, _ = _runtime(
        tmp_path,
        endpoint_factory=endpoint_factory,
    )
    start_errors: list[str] = []
    stop_results: list[dict[str, Any]] = []

    def start() -> None:
        try:
            runtime.start()
        except DsoftbusRuntimeError as error:
            start_errors.append(error.code)

    starter = threading.Thread(target=start)
    stopper = threading.Thread(
        target=lambda: stop_results.append(
            dict(runtime.stop(time.monotonic() + 5))
        )
    )
    starter.start()
    assert entered.wait(5)
    stopper.start()
    release.set()
    starter.join(5)
    stopper.join(5)
    assert start_errors == ["ENDPOINT_IN_USE"]
    assert stop_results[0]["state"] == RuntimeState.STOPPED
    assert runtime.state == RuntimeState.STOPPED
    assert driver.start_calls == []


def test_concurrent_stop_latches_earliest_deadline(tmp_path: Path) -> None:
    stop_entered = threading.Event()
    stop_release = threading.Event()
    driver = _FakeDriver(stop_entered=stop_entered, stop_release=stop_release)
    runtime, _, _ = _runtime(tmp_path, driver=driver)
    runtime.start()
    now = time.monotonic()
    later = now + 8
    earlier = now + 4
    failures: list[BaseException] = []

    def stop(deadline: float) -> None:
        try:
            runtime.stop(deadline)
        except BaseException as error:
            failures.append(error)

    leader = threading.Thread(target=stop, args=(later,))
    follower = threading.Thread(target=stop, args=(earlier,))
    leader.start()
    assert stop_entered.wait(5)
    follower.start()
    deadline = time.monotonic() + 2
    while runtime.shutdown_deadline != earlier and time.monotonic() < deadline:
        time.sleep(0.001)
    assert runtime.shutdown_deadline == earlier
    stop_release.set()
    leader.join(5)
    follower.join(5)
    assert not leader.is_alive() and not follower.is_alive()
    assert failures == []
    assert runtime.state == RuntimeState.STOPPED
    assert driver.stop_observed_deadlines == [earlier]


def test_invalid_driver_outcome_rolls_back_and_reraises(tmp_path: Path) -> None:
    driver = _FakeDriver(state=RuntimeState.READY, reasons=("WORKER_START_FAILED",))
    runtime, _, endpoint_factory = _runtime(tmp_path, driver=driver)
    with pytest.raises(DsoftbusRuntimeError) as caught:
        runtime.start()
    assert caught.value.code == "RUNTIME_DRIVER_INVALID"
    assert runtime.state == RuntimeState.NEW
    assert driver.begin_calls == 1
    assert len(driver.stop_calls) == 1
    assert endpoint_factory.lock.release_calls == 1


def test_provider_update_is_same_runtime_and_rolls_back_on_driver_error(
    tmp_path: Path,
) -> None:
    initial = object()
    replacement = object()
    driver = _FakeDriver()
    runtime, _, _ = _runtime(
        tmp_path, driver=driver, provider_runtime=initial
    )
    runtime.update_provider_runtime(replacement)
    assert runtime.health_snapshot()["providerReady"] is True
    assert driver.provider_updates == [replacement]
    driver.provider_error = RuntimeError("injected")
    with pytest.raises(RuntimeError, match="injected"):
        runtime.update_provider_runtime(None)
    assert runtime.health_snapshot()["providerReady"] is True
    runtime.stop(time.monotonic() + 5)
    with pytest.raises(DsoftbusRuntimeError) as caught:
        runtime.update_provider_runtime(object())
    assert caught.value.code == "RUNTIME_STOPPING"


def test_endpoint_holder_is_exact_canonical_and_immutable() -> None:
    runtime_id = str(uuid.uuid4())
    holder = build_endpoint_holder(runtime_id, pid=123, start_time_ticks=456)
    assert dict(holder) == {
        "pid": 123,
        "runtimeInstanceId": runtime_id,
        "schemaVersion": ENDPOINT_LOCK_SCHEMA,
        "startTimeTicks": 456,
    }
    raw = canonical_json_bytes(dict(holder))
    parsed = parse_endpoint_holder(raw)
    assert dict(parsed) == dict(holder)
    with pytest.raises(TypeError):
        parsed["pid"] = 1  # type: ignore[index]


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: {**value, "extra": True},
        lambda value: {**value, "pid": True},
        lambda value: {**value, "startTimeTicks": 0},
        lambda value: {**value, "schemaVersion": "wrong"},
    ],
)
def test_endpoint_holder_rejects_invalid_shape(mutator: Any) -> None:
    holder = dict(
        build_endpoint_holder(
            str(uuid.uuid4()), pid=123, start_time_ticks=456
        )
    )
    with pytest.raises(EndpointLockError) as caught:
        parse_endpoint_holder(canonical_json_bytes(mutator(holder)))
    assert caught.value.code == "ENDPOINT_LOCK_RECORD_INVALID"
    with pytest.raises(EndpointLockError):
        parse_endpoint_holder(
            json.dumps(holder, sort_keys=True).encode("utf-8")
        )


def test_non_posix_endpoint_lock_fails_before_state_mutation(tmp_path: Path) -> None:
    if os.name == "posix":
        pytest.skip("non-POSIX boundary")
    state_root = tmp_path / "dsoftbus"
    with pytest.raises(EndpointLockError) as caught:
        DsoftbusEndpointLock.acquire(
            state_root,
            str(uuid.uuid4()),
            pid=123,
            start_time_ticks=456,
        )
    assert caught.value.code == "POSIX_LOCK_PRIMITIVE_UNAVAILABLE"
    assert not state_root.exists()


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX flock and dir_fd")
def test_posix_endpoint_lock_excludes_contender_and_retains_record(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "dsoftbus"
    first_id = str(uuid.uuid4())
    first = DsoftbusEndpointLock.acquire(
        state_root,
        first_id,
        pid=os.getpid(),
        start_time_ticks=read_process_start_time_ticks(),
    )
    first.verify_current_path()
    with pytest.raises(EndpointLockError) as caught:
        DsoftbusEndpointLock.acquire(
            state_root,
            str(uuid.uuid4()),
            pid=os.getpid(),
            start_time_ticks=read_process_start_time_ticks(),
        )
    assert caught.value.code == "ENDPOINT_IN_USE"
    assert dict(parse_endpoint_holder(first.path.read_bytes())) == dict(first.holder)
    first.release()
    assert first.path.is_file()

    second_id = str(uuid.uuid4())
    second = DsoftbusEndpointLock.acquire(
        state_root,
        second_id,
        pid=os.getpid(),
        start_time_ticks=read_process_start_time_ticks(),
    )
    assert second.holder["runtimeInstanceId"] == second_id
    second.release()


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX flock and dir_fd")
def test_posix_endpoint_lock_detects_path_replacement(tmp_path: Path) -> None:
    state_root = tmp_path / "dsoftbus"
    lock = DsoftbusEndpointLock.acquire(
        state_root,
        str(uuid.uuid4()),
        pid=os.getpid(),
        start_time_ticks=read_process_start_time_ticks(),
    )
    replacement = state_root / "replacement"
    replacement.write_bytes(canonical_json_bytes(dict(lock.holder)))
    replacement.chmod(0o600)
    os.replace(replacement, lock.path)
    with pytest.raises(EndpointLockError) as caught:
        lock.verify_current_path()
    assert caught.value.code == "ENDPOINT_LOCK_OWNERSHIP_LOST"
    lock.release(require_current_path=False)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX no-follow open")
def test_posix_endpoint_lock_rejects_symlink_state_root(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    linked = tmp_path / "dsoftbus"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(EndpointLockError) as caught:
        DsoftbusEndpointLock.acquire(
            linked,
            str(uuid.uuid4()),
            pid=os.getpid(),
            start_time_ticks=read_process_start_time_ticks(),
        )
    assert caught.value.code == "ENDPOINT_STATE_ROOT_INVALID"
