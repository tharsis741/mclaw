# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Async owner-loop driver for DSoftBus Runtime mutable state."""

from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import math
import threading
import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Mapping, NoReturn, Protocol

from . import protocol
from .health import (
    DEGRADED_REASON_PRIORITY,
    RuntimeHealthPublisher,
)
from .provider_readiness import resolve_provider_readiness

_DEGRADED_REASONS = frozenset(DEGRADED_REASON_PRIORITY)
_START_KEYS = frozenset({"degradedReasons", "healthUpdates", "state"})


class OwnerLoopError(RuntimeError):
    """Stable owner-loop lifecycle failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _fail(code: str, cause: BaseException | None = None) -> NoReturn:
    error = OwnerLoopError(code)
    if cause is None:
        raise error
    raise error from cause


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return copy.deepcopy(value)


def _frozen(value: Mapping[str, Any]) -> Mapping[str, Any]:
    def freeze(item: Any) -> Any:
        if isinstance(item, Mapping):
            return MappingProxyType({key: freeze(child) for key, child in item.items()})
        if isinstance(item, (tuple, list)):
            return tuple(freeze(child) for child in item)
        return item

    return freeze(_plain(value))


class OwnerResources(Protocol):
    """Mutable resource adapter invoked exclusively by the owner loop."""

    async def start(self, runtime_instance_id: str) -> Mapping[str, Any]: ...

    async def begin_shutdown(self) -> Mapping[str, Any] | None: ...

    async def stop(
        self, deadline: Callable[[], float]
    ) -> Mapping[str, Any] | None: ...

    async def update_provider_runtime(
        self, context: Any | None
    ) -> Mapping[str, Any] | None: ...

    async def run_agent_task(
        self,
        device_id: str,
        text: str,
        *,
        context_id: str | None,
        message_id: str,
        event_sink: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> Mapping[str, Any]: ...

    def list_trusted_devices(self) -> tuple[Mapping[str, Any], ...]: ...

    async def discover_devices(self) -> tuple[Mapping[str, Any], ...]: ...

    async def pair_device(
        self, device_id_sha256: str
    ) -> Mapping[str, Any]: ...

    async def unbind_device(
        self, device_id_sha256: str
    ) -> Mapping[str, Any]: ...

    def local_turn_started(self, token: str) -> None: ...

    def local_turn_finished(self, token: str) -> None: ...

    def emergency_reap(self) -> None: ...


class AdmissionOnlyOwnerResources:
    """Safe owner-loop resource set until the Worker supervisor is attached."""

    async def start(self, runtime_instance_id: str) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "degradedReasons": ("PRODUCT_INTEGRATION_UNVERIFIED",),
                "healthUpdates": MappingProxyType({}),
                "state": "DEGRADED",
            }
        )

    async def begin_shutdown(self) -> Mapping[str, Any] | None:
        return MappingProxyType({})

    async def stop(
        self, deadline: Callable[[], float]
    ) -> Mapping[str, Any] | None:
        return MappingProxyType({})

    async def update_provider_runtime(
        self, context: Any | None
    ) -> Mapping[str, Any] | None:
        return MappingProxyType({})

    async def run_agent_task(
        self,
        device_id: str,
        text: str,
        *,
        context_id: str | None,
        message_id: str,
        event_sink: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> Mapping[str, Any]:
        _fail("PEER_NOT_READY")

    def list_trusted_devices(self) -> tuple[Mapping[str, Any], ...]:
        _fail("WORKER_NOT_READY")

    async def discover_devices(self) -> tuple[Mapping[str, Any], ...]:
        _fail("WORKER_NOT_READY")

    async def pair_device(
        self, device_id_sha256: str
    ) -> Mapping[str, Any]:
        _fail("WORKER_NOT_READY")

    async def unbind_device(
        self, device_id_sha256: str
    ) -> Mapping[str, Any]:
        _fail("WORKER_NOT_READY")

    def local_turn_started(self, token: str) -> None:
        return None

    def local_turn_finished(self, token: str) -> None:
        return None

    def emergency_reap(self) -> None:
        return None


@dataclass(frozen=True)
class _StartOutcome:
    state: str
    reasons: tuple[str, ...]
    updates: Mapping[str, Any]


def _validate_updates(value: Any) -> Mapping[str, Any]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        _fail("OWNER_RESOURCE_RESULT_INVALID")
    return MappingProxyType(copy.deepcopy(dict(value)))


_RESOURCE_LIFECYCLE_STATE = "_lifecycleState"
_RESOURCE_DEGRADED_REASONS = "_degradedReasons"


def _validate_start(value: Any) -> _StartOutcome:
    if not isinstance(value, Mapping) or frozenset(value) != _START_KEYS:
        _fail("OWNER_RESOURCE_RESULT_INVALID")
    state = value["state"]
    reasons_value = value["degradedReasons"]
    if not isinstance(reasons_value, (tuple, list)):
        _fail("OWNER_RESOURCE_RESULT_INVALID")
    reasons = tuple(reasons_value)
    if (
        any(not isinstance(reason, str) for reason in reasons)
        or len(reasons) != len(set(reasons))
        or any(reason not in _DEGRADED_REASONS for reason in reasons)
        or tuple(reason for reason in DEGRADED_REASON_PRIORITY if reason in reasons)
        != reasons
    ):
        _fail("OWNER_RESOURCE_RESULT_INVALID")
    if state == "READY" and reasons:
        _fail("OWNER_RESOURCE_RESULT_INVALID")
    if state == "DEGRADED" and not reasons:
        _fail("OWNER_RESOURCE_RESULT_INVALID")
    if state not in {"READY", "DEGRADED"}:
        _fail("OWNER_RESOURCE_RESULT_INVALID")
    return _StartOutcome(state, reasons, _validate_updates(value["healthUpdates"]))


def _default_provider_readiness(context: Any | None) -> tuple[bool, str]:
    return resolve_provider_readiness(context)


class DsoftbusOwnerLoopDriver:
    """Runtime driver that owns one asyncio loop and persistent health producer."""

    def __init__(
        self,
        *,
        config: Mapping[str, Any],
        provider_runtime: Any | None,
        socket_cap: int,
        resources: OwnerResources | None = None,
        provider_readiness: Callable[[Any | None], tuple[bool, str]] = (
            _default_provider_readiness
        ),
        publisher_factory: Callable[..., RuntimeHealthPublisher] = (
            RuntimeHealthPublisher
        ),
        loop_factory: Callable[[], asyncio.AbstractEventLoop] = (
            asyncio.new_event_loop
        ),
        monotonic: Callable[[], float] = time.monotonic,
        start_timeout: float = 5.0,
        resource_start_timeout: float | None = None,
    ) -> None:
        if not isinstance(config, Mapping):
            raise TypeError("config must be a mapping")
        if isinstance(start_timeout, bool) or not isinstance(start_timeout, (int, float)):
            raise TypeError("start_timeout must be a finite positive number")
        normalized_timeout = float(start_timeout)
        if not math.isfinite(normalized_timeout) or normalized_timeout <= 0:
            raise ValueError("start_timeout must be a finite positive number")
        if resource_start_timeout is None:
            normalized_resource_start_timeout = normalized_timeout
        else:
            if isinstance(resource_start_timeout, bool) or not isinstance(
                resource_start_timeout, (int, float)
            ):
                raise TypeError(
                    "resource_start_timeout must be a finite positive number"
                )
            normalized_resource_start_timeout = float(resource_start_timeout)
            if (
                not math.isfinite(normalized_resource_start_timeout)
                or normalized_resource_start_timeout <= 0
            ):
                raise ValueError(
                    "resource_start_timeout must be a finite positive number"
                )

        self._config = copy.deepcopy(dict(config))
        self._provider_runtime = provider_runtime
        self._socket_cap = socket_cap
        self._resources: OwnerResources = resources or AdmissionOnlyOwnerResources()
        self._provider_readiness = provider_readiness
        self._publisher_factory = publisher_factory
        self._loop_factory = loop_factory
        self._monotonic = monotonic
        self._start_timeout = normalized_timeout
        self._resource_start_timeout = normalized_resource_start_timeout
        self._condition = threading.Condition(threading.RLock())
        self._boot_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._owner_thread_id: int | None = None
        self._publisher: RuntimeHealthPublisher | None = None
        self._runtime_instance_id: str | None = None
        self._endpoint: Any | None = None
        self._boot_error: BaseException | None = None
        self._current_state = "STARTING"
        self._current_reasons: tuple[str, ...] = ()
        self._health_snapshot: Mapping[str, Any] | None = None
        self._resource_health_callback_enabled = False
        self._begin_complete = False
        self._stop_complete = False
        self._emergency_reaped = False

    @property
    def owner_thread_id(self) -> int | None:
        with self._condition:
            return self._owner_thread_id

    @property
    def thread(self) -> threading.Thread | None:
        with self._condition:
            return self._thread

    def _require_host_thread(self) -> None:
        with self._condition:
            owner_thread_id = self._owner_thread_id
        if owner_thread_id is not None and threading.get_ident() == owner_thread_id:
            _fail("OWNER_LOOP_REENTRANCY")

    def _cache_health(self, snapshot: Mapping[str, Any]) -> None:
        with self._condition:
            self._health_snapshot = _frozen(snapshot)
            self._current_state = str(snapshot["state"])
            self._current_reasons = tuple(snapshot["degradedReasons"])
            self._condition.notify_all()

    def _set_resource_health_callback(
        self,
        callback: Callable[[Mapping[str, Any]], None] | None,
    ) -> None:
        setter = getattr(self._resources, "set_health_change_callback", None)
        if setter is None:
            return
        if not callable(setter):
            _fail("OWNER_RESOURCE_RESULT_INVALID")
        setter(callback)

    def _publish_resource_health_change(self, value: Mapping[str, Any]) -> None:
        """Publish one resource update only when its health meaning changed."""

        if threading.get_ident() != self._owner_thread_id:
            _fail("OWNER_RESOURCE_CALLBACK_THREAD_INVALID")
        if (
            not self._resource_health_callback_enabled
            or self._begin_complete
            or self._stop_complete
        ):
            return
        assert self._publisher is not None
        raw_updates = dict(_validate_updates(value))
        requested_state = raw_updates.pop(
            _RESOURCE_LIFECYCLE_STATE, self._current_state
        )
        requested_reasons = raw_updates.pop(
            _RESOURCE_DEGRADED_REASONS, self._current_reasons
        )
        transition = _validate_start(
            {
                "degradedReasons": requested_reasons,
                "healthUpdates": raw_updates,
                "state": requested_state,
            }
        )
        updates = transition.updates
        with self._condition:
            current = self._health_snapshot
        if current is None:
            _fail("RUNTIME_HEALTH_NOT_PUBLISHED")
        if (
            transition.state == self._current_state
            and transition.reasons == self._current_reasons
            and (
                not updates
                or all(
                    key in current and _plain(current[key]) == _plain(update)
                    for key, update in updates.items()
                )
            )
        ):
            return
        snapshot = self._publisher.publish(
            transition.state,
            degraded_reasons=transition.reasons,
            updates=updates,
        )
        self._cache_health(snapshot)

    def _attach_resource_health_callback(self) -> None:
        self._resource_health_callback_enabled = True
        try:
            self._set_resource_health_callback(self._publish_resource_health_change)
        except BaseException:
            self._resource_health_callback_enabled = False
            try:
                self._set_resource_health_callback(None)
            except BaseException:
                pass
            raise

    def _detach_resource_health_callback(self) -> None:
        self._resource_health_callback_enabled = False
        self._set_resource_health_callback(None)

    def _thread_main(self) -> None:
        loop: asyncio.AbstractEventLoop | None = None
        try:
            loop = self._loop_factory()
            if not isinstance(loop, asyncio.AbstractEventLoop):
                _fail("OWNER_LOOP_START_FAILED")
            asyncio.set_event_loop(loop)
            with self._condition:
                self._loop = loop
                self._owner_thread_id = threading.get_ident()
            assert self._runtime_instance_id is not None
            provider_ready, provider_code = self._provider_readiness(
                self._provider_runtime
            )
            publisher = self._publisher_factory(
                endpoint=self._endpoint,
                runtime_instance_id=self._runtime_instance_id,
                config=self._config,
                socket_cap=self._socket_cap,
                provider_ready=provider_ready,
                provider_readiness_code=provider_code,
            )
            with self._condition:
                self._publisher = publisher
            self._cache_health(publisher.publish("STARTING"))
        except BaseException as error:
            with self._condition:
                self._boot_error = error
            self._boot_event.set()
            if loop is not None:
                loop.close()
            return
        self._boot_event.set()
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            loop.close()
            with self._condition:
                self._loop = None
                self._condition.notify_all()

    def _live_loop(self) -> asyncio.AbstractEventLoop:
        with self._condition:
            loop = self._loop
            thread = self._thread
        if loop is None or thread is None or not thread.is_alive():
            _fail("OWNER_LOOP_DIED")
        return loop

    def outbound_loop(self) -> asyncio.AbstractEventLoop:
        """Return the live owner loop to the existing outbound fence bridge."""

        self._require_host_thread()
        return self._live_loop()

    async def refresh_device_context_on_owner(
        self,
        device_id: str,
    ) -> Mapping[str, Any]:
        if threading.get_ident() != self._owner_thread_id:
            _fail("OWNER_RESOURCE_THREAD_INVALID")
        accessor = getattr(self._resources, "refresh_device_context", None)
        if not callable(accessor):
            _fail("PEER_NOT_READY")
        value = await accessor(device_id)
        if not isinstance(value, Mapping):
            _fail("OWNER_RESOURCE_RESULT_INVALID")
        return _frozen(value)

    async def run_agent_task_on_owner(
        self,
        device_id: str,
        text: str,
        *,
        context_id: str | None,
        message_id: str,
        event_sink: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> Mapping[str, Any]:
        if threading.get_ident() != self._owner_thread_id:
            _fail("OWNER_RESOURCE_THREAD_INVALID")
        accessor = getattr(self._resources, "run_agent_task", None)
        if not callable(accessor):
            _fail("PEER_NOT_READY")
        value = await accessor(
            device_id,
            text,
            context_id=context_id,
            message_id=message_id,
            event_sink=event_sink,
        )
        if not isinstance(value, Mapping):
            _fail("OWNER_RESOURCE_RESULT_INVALID")
        return _frozen(value)

    def _marshal(
        self,
        operation: Awaitable[Any],
        *,
        timeout: float,
    ) -> Any:
        self._require_host_thread()
        try:
            loop = self._live_loop()
        except BaseException:
            close = getattr(operation, "close", None)
            if callable(close):
                close()
            raise
        future = asyncio.run_coroutine_threadsafe(operation, loop)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as error:
            future.cancel()
            _fail("OWNER_LOOP_TIMEOUT", error)
        except OwnerLoopError:
            raise
        except BaseException:
            raise

    async def _start_resources(self) -> Mapping[str, Any]:
        assert self._publisher is not None
        assert self._runtime_instance_id is not None
        try:
            outcome = _validate_start(
                await self._resources.start(self._runtime_instance_id)
            )
        except OwnerLoopError:
            await self._rollback_resource_start()
            snapshot = self._publisher.publish(
                "DEGRADED",
                degraded_reasons=("HEALTH_INVARIANT_VIOLATION",),
            )
            self._cache_health(snapshot)
            raise
        except BaseException:
            await self._rollback_resource_start()
            snapshot = self._publisher.publish(
                "DEGRADED",
                degraded_reasons=("HEALTH_INVARIANT_VIOLATION",),
            )
            self._cache_health(snapshot)
            raise
        snapshot = self._publisher.publish(
            outcome.state,
            degraded_reasons=outcome.reasons,
            updates=outcome.updates,
        )
        self._cache_health(snapshot)
        self._attach_resource_health_callback()
        return MappingProxyType(
            {"degradedReasons": outcome.reasons, "state": outcome.state}
        )

    async def _rollback_resource_start(self) -> None:
        try:
            await self._resources.begin_shutdown()
        except BaseException:
            pass
        try:
            deadline = self._monotonic() + self._start_timeout
            await self._resources.stop(lambda: deadline)
        except BaseException:
            pass

    def start(self, runtime_instance_id: str, endpoint: Any) -> Mapping[str, Any]:
        """Start the one owner thread, publish STARTING, then start resources."""
        self._require_host_thread()
        with self._condition:
            if self._thread is not None:
                _fail("OWNER_LOOP_ALREADY_STARTED")
            self._runtime_instance_id = runtime_instance_id
            self._endpoint = endpoint
            self._thread = threading.Thread(
                target=self._thread_main,
                name="mclaw-dsoftbus-owner",
                daemon=False,
            )
            thread = self._thread
        try:
            thread.start()
        except BaseException as error:
            _fail("OWNER_LOOP_START_FAILED", error)
        if not self._boot_event.wait(self._start_timeout):
            self._emergency_reap()
            _fail("OWNER_LOOP_START_TIMEOUT")
        with self._condition:
            boot_error = self._boot_error
        if boot_error is not None:
            thread.join(self._start_timeout)
            _fail("OWNER_LOOP_START_FAILED", boot_error)
        return self._marshal(
            self._start_resources(), timeout=self._resource_start_timeout
        )

    async def _begin_shutdown_on_owner(self) -> None:
        if self._begin_complete:
            return
        assert self._publisher is not None
        updates: Mapping[str, Any] = MappingProxyType({})
        error: BaseException | None = None
        try:
            self._detach_resource_health_callback()
        except BaseException as caught:
            error = caught
        try:
            updates = _validate_updates(await self._resources.begin_shutdown())
        except BaseException as caught:
            if error is None:
                error = caught
        snapshot = self._publisher.publish("STOPPING", updates=updates)
        self._cache_health(snapshot)
        self._begin_complete = True
        if error is not None:
            raise error

    def begin_shutdown(self) -> None:
        self._require_host_thread()
        with self._condition:
            if self._stop_complete or self._begin_complete:
                return
        self._marshal(
            self._begin_shutdown_on_owner(), timeout=self._start_timeout
        )

    async def _stop_on_owner(
        self, deadline: Callable[[], float]
    ) -> Mapping[str, Any]:
        if self._stop_complete:
            assert self._health_snapshot is not None
            return self._health_snapshot
        if not self._begin_complete:
            await self._begin_shutdown_on_owner()
        assert self._publisher is not None
        updates = _validate_updates(await self._resources.stop(deadline))
        snapshot = self._publisher.publish("STOPPED", updates=updates)
        self._cache_health(snapshot)
        self._stop_complete = True
        return snapshot

    def _emergency_reap(self) -> None:
        with self._condition:
            if self._emergency_reaped:
                return
            self._emergency_reaped = True
        try:
            self._resources.emergency_reap()
        except BaseException:
            pass

    def _wait_future_until_deadline(
        self,
        future: concurrent.futures.Future[Any],
        deadline: Callable[[], float],
    ) -> Any:
        while True:
            remaining = max(0.0, deadline() - self._monotonic())
            try:
                return future.result(timeout=min(0.05, remaining))
            except concurrent.futures.TimeoutError:
                if remaining <= 0:
                    future.cancel()
                    _fail("OWNER_LOOP_STOP_TIMEOUT")

    def stop(self, deadline: Callable[[], float]) -> None:
        """Publish final STOPPED on owner, then stop/join the owner thread."""
        self._require_host_thread()
        try:
            loop = self._live_loop()
        except OwnerLoopError:
            self._emergency_reap()
            raise
        future = asyncio.run_coroutine_threadsafe(
            self._stop_on_owner(deadline), loop
        )
        try:
            self._wait_future_until_deadline(future, deadline)
        except BaseException:
            self._emergency_reap()
            try:
                loop.call_soon_threadsafe(loop.stop)
            except BaseException:
                pass
            raise
        loop.call_soon_threadsafe(loop.stop)
        with self._condition:
            thread = self._thread
        assert thread is not None
        while thread.is_alive():
            remaining = max(0.0, deadline() - self._monotonic())
            if remaining <= 0:
                self._emergency_reap()
                _fail("OWNER_LOOP_STOP_TIMEOUT")
            thread.join(min(0.05, remaining))

    async def _update_provider_on_owner(
        self,
        context: Any | None,
        provider_ready: bool,
        provider_code: str,
    ) -> None:
        assert self._publisher is not None
        try:
            updates = _validate_updates(
                await self._resources.update_provider_runtime(context)
            )
        except BaseException:
            snapshot = self._publisher.publish(
                self._current_state,
                degraded_reasons=self._current_reasons,
                updates={},
                provider_ready=False,
                provider_readiness_code="PROVIDER_SYNC_FAILED",
            )
            self._cache_health(snapshot)
            raise
        snapshot = self._publisher.publish(
            self._current_state,
            degraded_reasons=self._current_reasons,
            updates=updates,
            provider_ready=provider_ready,
            provider_readiness_code=provider_code,
        )
        self._provider_runtime = context
        self._cache_health(snapshot)

    def update_provider_runtime(self, context: Any | None) -> None:
        self._require_host_thread()
        provider_ready, provider_code = self._provider_readiness(context)
        self._marshal(
            self._update_provider_on_owner(
                context, provider_ready, provider_code
            ),
            timeout=self._start_timeout,
        )

    async def _local_turn_signal_on_owner(
        self,
        token: str,
        *,
        started: bool,
    ) -> None:
        name = "local_turn_started" if started else "local_turn_finished"
        accessor = getattr(self._resources, name, None)
        if accessor is None:
            return
        if not callable(accessor):
            _fail("OWNER_RESOURCE_RESULT_INVALID")
        result = accessor(token)
        if hasattr(result, "__await__"):
            await result

    def local_turn_started(self, token: str) -> None:
        self._require_host_thread()
        self._marshal(
            self._local_turn_signal_on_owner(token, started=True),
            timeout=self._start_timeout,
        )

    def local_turn_finished(self, token: str) -> None:
        self._require_host_thread()
        self._marshal(
            self._local_turn_signal_on_owner(token, started=False),
            timeout=self._start_timeout,
        )

    def health_snapshot(self) -> Mapping[str, Any]:
        """Return an independent immutable cached snapshot without loop I/O."""
        with self._condition:
            snapshot = self._health_snapshot
        if snapshot is None:
            _fail("RUNTIME_HEALTH_NOT_PUBLISHED")
        return _frozen(snapshot)

    def diagnostic_snapshot(self) -> Mapping[str, Any]:
        """Return only the resource adapter's bounded, non-sensitive cache."""

        diagnostic = getattr(self._resources, "cached_diagnostic", None)
        if not callable(diagnostic):
            return MappingProxyType({})
        value = diagnostic()
        if not isinstance(value, Mapping):
            _fail("OWNER_RESOURCE_RESULT_INVALID")
        return _frozen(value)

    def public_peers_snapshot(self) -> tuple[Mapping[str, Any], ...]:
        """Return verified public Peer cache entries without owner-loop I/O."""

        accessor = getattr(self._resources, "cached_public_peers", None)
        if not callable(accessor):
            return ()
        value = accessor()
        if not isinstance(value, (list, tuple)) or any(
            not isinstance(item, Mapping) for item in value
        ):
            _fail("OWNER_RESOURCE_RESULT_INVALID")
        return tuple(_frozen(item) for item in value)

    async def _list_trusted_devices_on_owner(
        self,
    ) -> tuple[Mapping[str, Any], ...]:
        accessor = getattr(self._resources, "list_trusted_devices", None)
        if not callable(accessor):
            _fail("WORKER_NOT_READY")
        value = accessor()
        if hasattr(value, "__await__"):
            value = await value
        if not isinstance(value, (list, tuple)) or any(
            not isinstance(item, Mapping) for item in value
        ):
            _fail("OWNER_RESOURCE_RESULT_INVALID")
        return tuple(_frozen(item) for item in value)

    def list_trusted_devices(self) -> tuple[Mapping[str, Any], ...]:
        """Run one bounded DeviceManager enumeration on the owner loop."""

        return self._marshal(
            self._list_trusted_devices_on_owner(),
            timeout=float(protocol.CONTROL_TIMEOUT_S) + 0.5,
        )

    async def _discover_devices_on_owner(
        self,
    ) -> tuple[Mapping[str, Any], ...]:
        accessor = getattr(self._resources, "discover_devices", None)
        if not callable(accessor):
            _fail("WORKER_NOT_READY")
        value = accessor()
        if hasattr(value, "__await__"):
            value = await value
        if not isinstance(value, (list, tuple)) or any(
            not isinstance(item, Mapping) for item in value
        ):
            _fail("OWNER_RESOURCE_RESULT_INVALID")
        return tuple(_frozen(item) for item in value)

    def discover_devices(self) -> tuple[Mapping[str, Any], ...]:
        """Run one bounded DeviceManager discovery on the owner loop."""

        return self._marshal(
            self._discover_devices_on_owner(),
            timeout=(
                float(protocol.DEVICE_DISCOVERY_WINDOW_S)
                + float(protocol.CONTROL_TIMEOUT_S) * 2
                + 0.5
            ),
        )

    async def _pair_device_on_owner(
        self, device_id_sha256: str
    ) -> Mapping[str, Any]:
        accessor = getattr(self._resources, "pair_device", None)
        if not callable(accessor):
            _fail("WORKER_NOT_READY")
        value = accessor(device_id_sha256)
        if hasattr(value, "__await__"):
            value = await value
        if not isinstance(value, Mapping):
            _fail("OWNER_RESOURCE_RESULT_INVALID")
        return _frozen(value)

    def pair_device(self, device_id_sha256: str) -> Mapping[str, Any]:
        """Run one non-replayed, system-confirmed bind on the owner loop."""

        return self._marshal(
            self._pair_device_on_owner(device_id_sha256),
            timeout=(
                float(protocol.DEVICE_BIND_TIMEOUT_S)
                + float(protocol.CONTROL_TIMEOUT_S) * 2
                + 0.5
            ),
        )

    async def _unbind_device_on_owner(
        self, device_id_sha256: str
    ) -> Mapping[str, Any]:
        accessor = getattr(self._resources, "unbind_device", None)
        if not callable(accessor):
            _fail("WORKER_NOT_READY")
        value = accessor(device_id_sha256)
        if hasattr(value, "__await__"):
            value = await value
        if not isinstance(value, Mapping):
            _fail("OWNER_RESOURCE_RESULT_INVALID")
        return _frozen(value)

    def unbind_device(self, device_id_sha256: str) -> Mapping[str, Any]:
        """Run one non-replayed trust mutation on the owner loop."""

        return self._marshal(
            self._unbind_device_on_owner(device_id_sha256),
            timeout=float(protocol.CONTROL_TIMEOUT_S) * 3 + 0.5,
        )

    def cached_device_context(self, device_id: str) -> Mapping[str, Any]:
        """Return one resource-owned immutable cache entry without loop I/O."""

        accessor = getattr(self._resources, "cached_device_context", None)
        if not callable(accessor):
            _fail("PEER_NOT_READY")
        value = accessor(device_id)
        if not isinstance(value, Mapping):
            _fail("OWNER_RESOURCE_RESULT_INVALID")
        return _frozen(value)

    async def refresh_device_context_async(
        self, device_id: str
    ) -> Mapping[str, Any]:
        """Await a bounded owner-loop refresh without creating a helper thread."""

        self._require_host_thread()
        accessor = getattr(self._resources, "refresh_device_context", None)
        if not callable(accessor):
            _fail("PEER_NOT_READY")
        loop = self._live_loop()
        operation = accessor(device_id)
        if not hasattr(operation, "__await__"):
            _fail("OWNER_RESOURCE_RESULT_INVALID")
        future = asyncio.run_coroutine_threadsafe(operation, loop)
        try:
            value = await asyncio.wait_for(
                asyncio.wrap_future(future),
                timeout=float(protocol.CONTROL_TIMEOUT_S) + 0.5,
            )
        except TimeoutError as error:
            future.cancel()
            _fail("DEADLINE_EXCEEDED", error)
        if not isinstance(value, Mapping):
            _fail("OWNER_RESOURCE_RESULT_INVALID")
        return _frozen(value)

    async def run_agent_task_async(
        self,
        device_id: str,
        text: str,
        *,
        context_id: str | None,
        message_id: str,
        event_sink: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> Mapping[str, Any]:
        """Await one Task without imposing an owner-side wall-clock limit."""

        self._require_host_thread()
        accessor = getattr(self._resources, "run_agent_task", None)
        if not callable(accessor):
            _fail("PEER_NOT_READY")
        loop = self._live_loop()
        operation = accessor(
            device_id,
            text,
            context_id=context_id,
            message_id=message_id,
            event_sink=event_sink,
        )
        if not hasattr(operation, "__await__"):
            _fail("OWNER_RESOURCE_RESULT_INVALID")
        future = asyncio.run_coroutine_threadsafe(operation, loop)
        value = await asyncio.wrap_future(future)
        if not isinstance(value, Mapping):
            _fail("OWNER_RESOURCE_RESULT_INVALID")
        return _frozen(value)

__all__ = [
    "AdmissionOnlyOwnerResources",
    "DsoftbusOwnerLoopDriver",
    "OwnerLoopError",
    "OwnerResources",
]
