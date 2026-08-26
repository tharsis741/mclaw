# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import sys
import threading
import time
from types import MappingProxyType
from typing import Any, Mapping
import uuid

import pytest

from mclaw.dsoftbus import protocol
from mclaw.dsoftbus.discovery_resources import DiscoveryOwnerResources
from mclaw.dsoftbus.owner import DsoftbusOwnerLoopDriver
from mclaw.dsoftbus.presence import (
    DiscoveredNode,
    InMemoryPresenceAdapter,
    PresenceError,
    derive_public_device_id,
)
from mclaw.dsoftbus.runtime import DsoftbusRuntime, RuntimeState
from mclaw.dsoftbus.worker_supervisor import (
    SubprocessWorkerLauncher,
    WorkerIdentityExpectation,
    WorkerSupervisor,
    WorkerSupervisorError,
)


_FAKE_WORKER = (
    Path(__file__).parent
    / "fixtures"
    / "dsoftbus"
    / "fake_worker_target.py"
).resolve()
_TOKEN_HASH = "sha256:" + "a" * 64


def _hello(
    *,
    local_udid: str = "local-device",
    uid: int = 0,
    socket_cap: int = 16,
) -> dict[str, Any]:
    return {
        "identity": {
            "capabilitySet": ["CAP_NET_RAW"],
            "distributedDataSyncGranted": True,
            "gid": 0,
            "selinuxDomain": "u:r:su:s0",
            "supplementaryGids": [1006, 1007],
            "tokenIdHash": _TOKEN_HASH,
            "uid": uid,
        },
        "localUdid": local_udid,
        "nativeAbiVersion": 1,
        "socketCap": socket_cap,
    }


def _policy(*, expected_device_id: str | None = None) -> WorkerIdentityExpectation:
    return WorkerIdentityExpectation(
        uid=0,
        gid=0,
        supplementary_gids=(1006, 1007),
        capability_set=("CAP_NET_RAW",),
        token_id_hash=_TOKEN_HASH,
        selinux_domain="u:r:su:s0",
        socket_cap=16,
        expected_device_id=expected_device_id,
    )


def _encoded_hello(value: Mapping[str, Any]) -> str:
    raw = (
        json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def _launcher(
    *,
    mode: str = "normal",
    epoch: str | None = None,
    hello: Mapping[str, Any] | None = None,
    audit_path: Path | None = None,
    exit_delay: float = 0.10,
    stop_delay: float = 0.0,
) -> SubprocessWorkerLauncher:
    epoch = epoch or str(uuid.uuid4())
    argv = [
        sys.executable,
        str(_FAKE_WORKER),
        "--epoch",
        epoch,
        "--hello-json-base64",
        _encoded_hello(hello or _hello()),
        "--mode",
        mode,
        "--exit-delay",
        str(exit_delay),
        "--stop-delay",
        str(stop_delay),
    ]
    if audit_path is not None:
        argv.extend(("--audit-path", str(audit_path)))
    return SubprocessWorkerLauncher(
        argv=tuple(argv),
        environment=dict(os.environ),
        start_time_reader=lambda pid: pid + 10_000,
    )


def _supervisor(
    *,
    mode: str = "normal",
    launcher: Any | None = None,
    policy: WorkerIdentityExpectation | None = None,
    control_timeout: float = 1.0,
    thread_factory: Any = threading.Thread,
) -> WorkerSupervisor:
    return WorkerSupervisor(
        launcher=launcher or _launcher(mode=mode),
        identity=policy or _policy(),
        control_timeout=control_timeout,
        thread_factory=thread_factory,
    )


def _wait_for(predicate: Any, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition was not reached")
        time.sleep(0.01)


def test_supervisor_allows_only_hello_then_reserved_stop_and_reaps() -> None:
    supervisor = _supervisor()
    verified = supervisor.start()
    assert verified.public_device_id == derive_public_device_id("local-device")
    assert verified.public_agent_id.endswith(verified.public_device_id.rsplit(":", 1)[-1])
    health = supervisor.health_updates()
    assert health["workerAlive"] is True
    assert health["workerPid"] != os.getpid()
    assert health["workerStartTimeTicks"] == health["workerPid"] + 10_000
    assert len(health["workerEpoch"]) == 12
    before = supervisor.diagnostic_snapshot()
    assert before["activeThreadCount"] == 4
    assert before["operationCounts"]["hello"] == 1
    assert all(
        before["operationCounts"][operation] == 0
        for operation in {"start", "snapshot_nodes", "listen", "connect"}
    )

    supervisor.begin_shutdown()
    supervisor.stop(time.monotonic() + 2)
    final = supervisor.health_updates()
    assert final["workerAlive"] is False
    assert final["workerPid"] == health["workerPid"]
    assert final["parentCommandQueueCount"] == 0
    assert final["parentResponseRouteCount"] == 0
    after = supervisor.diagnostic_snapshot()
    assert after["activeThreadCount"] == 0
    assert after["operationCounts"]["stop"] == 1


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("startup-error", "WORKER_START_FAILED"),
        ("malformed-readiness", "WORKER_PROTOCOL_ERROR"),
        ("bad-readiness", "WORKER_PROTOCOL_ERROR"),
        ("wrong-ready-pid", "WORKER_PROTOCOL_ERROR"),
        ("malformed-hello", "WORKER_PROTOCOL_ERROR"),
        ("wrong-hello-epoch", "WORKER_PROTOCOL_ERROR"),
    ],
)
def test_startup_failures_are_reaped_and_never_retried(mode: str, expected: str) -> None:
    supervisor = _supervisor(mode=mode, control_timeout=0.5)
    with pytest.raises(WorkerSupervisorError) as caught:
        supervisor.start()
    assert caught.value.code == expected
    assert supervisor.diagnostic_snapshot()["activeThreadCount"] == 0
    with pytest.raises(WorkerSupervisorError) as second:
        supervisor.start()
    assert second.value.code == "WORKER_START_ALREADY_ATTEMPTED"
    with pytest.raises(WorkerSupervisorError) as retry:
        supervisor.recover()
    assert retry.value.code == "WORKER_RESTART_NOT_ALLOWED"
    supervisor.stop(time.monotonic() + 1)


def test_hello_identity_mismatch_fails_closed_without_sensitive_diagnostic() -> None:
    supervisor = _supervisor(launcher=_launcher(hello=_hello(uid=1)))
    with pytest.raises(WorkerSupervisorError) as caught:
        supervisor.start()
    assert caught.value.code == "WORKER_IDENTITY_MISMATCH"
    diagnostic = supervisor.diagnostic_snapshot()
    assert "local-device" not in repr(diagnostic)
    assert diagnostic["operationCounts"]["hello"] == 1
    assert diagnostic["activeThreadCount"] == 0
    supervisor.stop(time.monotonic() + 1)


def test_expected_public_identity_is_frozen() -> None:
    wrong = derive_public_device_id("another-device")
    supervisor = _supervisor(policy=_policy(expected_device_id=wrong))
    with pytest.raises(WorkerSupervisorError) as caught:
        supervisor.start()
    assert caught.value.code == "WORKER_IDENTITY_MISMATCH"
    supervisor.stop(time.monotonic() + 1)


def test_unexpected_event_before_native_start_poison_epoch() -> None:
    supervisor = _supervisor(mode="event-after-hello")
    supervisor.start()
    _wait_for(lambda: not supervisor.health_updates()["workerAlive"])
    assert supervisor.health_updates()["parentEventDepth"] == 0
    supervisor.stop(time.monotonic() + 1)
    assert supervisor.diagnostic_snapshot()["activeThreadCount"] == 0


def test_hung_stop_uses_terminate_kill_reap_and_joins_all_threads() -> None:
    # Keep process startup independent from the deliberately short stop budget.
    # A loaded Windows host can need more than 100 ms to spawn the interpreter;
    # the 250 ms absolute stop deadline below is the behavior under test.
    supervisor = _supervisor(mode="hang-stop", control_timeout=0.50)
    supervisor.start()
    started = time.monotonic()
    supervisor.stop(started + 0.25)
    assert time.monotonic() - started < 2
    diagnostic = supervisor.diagnostic_snapshot()
    assert diagnostic["activeThreadCount"] == 0
    assert diagnostic["operationCounts"]["stop"] == 1
    assert supervisor.health_updates()["workerAlive"] is False


def test_partial_thread_start_rolls_back_process_and_started_threads() -> None:
    calls = 0

    class FailingThread(threading.Thread):
        def start(self) -> None:
            raise RuntimeError("injected")

    def factory(**kwargs: Any) -> threading.Thread:
        nonlocal calls
        calls += 1
        if calls == 2:
            return FailingThread(**kwargs)
        return threading.Thread(**kwargs)

    supervisor = _supervisor(thread_factory=factory)
    with pytest.raises(WorkerSupervisorError) as caught:
        supervisor.start()
    assert caught.value.code == "WORKER_THREAD_START_FAILED"
    assert supervisor.diagnostic_snapshot()["activeThreadCount"] == 0
    supervisor.stop(time.monotonic() + 1)


class _SequenceLauncher:
    def __init__(self, modes: list[str]) -> None:
        self._modes = modes
        self.calls = 0

    def spawn(self) -> Any:
        if self.calls >= len(self._modes):
            raise WorkerSupervisorError("WORKER_LAUNCH_FAILED")
        mode = self._modes[self.calls]
        self.calls += 1
        return _launcher(mode=mode, exit_delay=0.08).spawn()


def test_ready_epoch_can_recover_but_restart_ledger_exhausts_at_three() -> None:
    launcher = _SequenceLauncher(["exit-after-hello"] * 4)
    supervisor = _supervisor(launcher=launcher, control_timeout=0.5)
    first = supervisor.start()
    supervisor.enable_recovery_after_ready()
    for expected_count in range(1, protocol.WORKER_RESTART_LIMIT + 1):
        _wait_for(lambda: not supervisor.health_updates()["workerAlive"])
        recovered = supervisor.recover()
        assert recovered.public_device_id == first.public_device_id
        assert supervisor.health_updates()["restartCount"] == expected_count
    _wait_for(lambda: not supervisor.health_updates()["workerAlive"])
    with pytest.raises(WorkerSupervisorError) as caught:
        supervisor.recover()
    assert caught.value.code == "WORKER_RESTART_EXHAUSTED"
    assert supervisor.diagnostic_snapshot()["restartExhausted"] is True
    supervisor.stop(time.monotonic() + 1)
    assert supervisor.diagnostic_snapshot()["activeThreadCount"] == 0


def _node(index: int, *, prefix: str = "peer") -> DiscoveredNode:
    return DiscoveredNode(
        network_id=f"network-{prefix}-{index}",
        udid=f"udid-{prefix}-{index}",
        device_name=f"Device {index}",
        device_type_id=index % 65_536,
    )


def test_presence_snapshot_is_sorted_bounded_and_retains_no_raw_identity() -> None:
    adapter = InMemoryPresenceAdapter(
        local_device_id=derive_public_device_id("local"), socket_cap=16
    )
    epoch = str(uuid.uuid4())
    adapter.reset_worker_epoch(epoch)
    nodes = [_node(index) for index in range(65)]
    transitions = adapter.apply_snapshot(list(reversed(nodes)))
    expected = sorted(derive_public_device_id(node.udid) for node in nodes)
    assert [peer["deviceId"] for peer in adapter.public_peers()] == expected[:64]
    assert sum(transition.admitted for transition in transitions) == 64
    assert adapter.health_updates() == {
        "connectedPeerCount": 0,
        "peerCount": 64,
        "peerRegistryDropped": 1,
        "readyPeerCount": 0,
        "stateFreshPeerCount": 0,
    }
    rendered = repr(adapter.public_peers())
    assert "network-peer" not in rendered
    assert "udid-peer" not in rendered
    assert all(peer["agentAvailability"] == "UNAVAILABLE" for peer in adapter.public_peers())


def test_presence_stops_consuming_an_unbounded_snapshot_at_the_native_cap() -> None:
    adapter = InMemoryPresenceAdapter(
        local_device_id=derive_public_device_id("local"), socket_cap=16
    )
    adapter.reset_worker_epoch(str(uuid.uuid4()))

    def nodes() -> Any:
        index = 0
        while True:
            yield _node(index, prefix="unbounded")
            index += 1

    with pytest.raises(PresenceError) as caught:
        adapter.apply_snapshot(nodes())  # type: ignore[arg-type]
    assert caught.value.code == "NODE_SNAPSHOT_OVERFLOW"
    assert adapter.health_updates()["peerCount"] == 0


def test_presence_generation_event_order_offline_refresh_and_epoch_reset() -> None:
    adapter = InMemoryPresenceAdapter(
        local_device_id=derive_public_device_id("local"), socket_cap=16
    )
    first_epoch = str(uuid.uuid4())
    adapter.reset_worker_epoch(first_epoch)
    nodes = [_node(index) for index in range(65)]
    adapter.apply_snapshot(nodes)
    first_views = {peer["deviceId"]: peer for peer in adapter.public_peers()}
    adapter.apply_snapshot(nodes)
    assert {
        peer["deviceId"]: peer["generation"] for peer in adapter.public_peers()
    } == {device_id: peer["generation"] for device_id, peer in first_views.items()}

    admitted = adapter.public_peers()[0]
    source = next(node for node in nodes if derive_public_device_id(node.udid) == admitted["deviceId"])
    remaining = [node for node in nodes if node.network_id != source.network_id]
    offline = adapter.node_offline(
        source.network_id, node_event_seq=1, refresh_nodes=remaining
    )
    assert offline.presence == "OFFLINE"
    assert adapter.health_updates()["peerCount"] == 64
    with pytest.raises(PresenceError) as stale:
        adapter.node_online(_node(100), node_event_seq=1)
    assert stale.value.code == "PRESENCE_EVENT_SEQUENCE_INVALID"

    adapter.reset_worker_epoch(str(uuid.uuid4()))
    assert adapter.health_updates()["peerCount"] == 0
    transition = adapter.node_online(_node(100), node_event_seq=1)
    assert transition.admitted is True
    assert transition.generation > offline.generation


def test_connection_planner_is_deterministic_and_never_opens_a_socket() -> None:
    local_id = derive_public_device_id("local")
    adapter = InMemoryPresenceAdapter(local_device_id=local_id, socket_cap=4)
    adapter.reset_worker_epoch(str(uuid.uuid4()))
    adapter.apply_snapshot([_node(1), _node(2), _node(3)])
    plan = adapter.connection_plan()
    assert adapter.effective_peer_cap == 1
    assert [item["admitted"] for item in plan] == [True, False, False]
    assert [item["deviceId"] for item in plan] == sorted(
        item["deviceId"] for item in plan
    )
    assert all(
        item["action"]
        == ("INITIATE" if local_id < item["deviceId"] else "AWAIT_INBOUND")
        for item in plan
    )
    assert adapter.health_updates()["connectedPeerCount"] == 0


def _config() -> dict[str, Any]:
    return {
        "dsoftbus": {
            "accept_remote_messages": False,
            "enabled": "auto",
            "global_requests_per_minute": 12,
            "per_peer_requests_per_minute": 6,
            "remote_token_budget_per_hour": 100_000,
        }
    }


class _Endpoint:
    def __init__(self, runtime_instance_id: str) -> None:
        self.holder = MappingProxyType(
            {
                "pid": os.getpid(),
                "runtimeInstanceId": runtime_instance_id,
                "schemaVersion": "mclaw.dsoftbus.endpoint-lock/v1",
                "startTimeTicks": 123,
            }
        )
        self.payloads: list[bytes] = []
        self.released = False

    def publish_owned_runtime_health(self, raw: bytes, *, temp_name: str) -> None:
        self.payloads.append(raw)

    def release(self) -> None:
        self.released = True


def _runtime_resources(
    *, mode: str = "normal"
) -> tuple[DsoftbusRuntime, DsoftbusOwnerLoopDriver, DiscoveryOwnerResources]:
    supervisor = _supervisor(mode=mode, control_timeout=0.5)
    resources = DiscoveryOwnerResources(
        supervisor=supervisor,
        initial_nodes=(_node(1), _node(2)),
    )
    driver = DsoftbusOwnerLoopDriver(
        config=_config(),
        provider_runtime=None,
        socket_cap=16,
        resources=resources,
        start_timeout=2,
    )
    endpoints: list[_Endpoint] = []

    def endpoint_factory(state_root: Path, runtime_instance_id: str) -> _Endpoint:
        endpoint = _Endpoint(runtime_instance_id)
        endpoints.append(endpoint)
        return endpoint

    runtime = DsoftbusRuntime(
        provider_runtime=None,
        config=_config(),
        workspace=Path.cwd().resolve(),
        state_root=Path.cwd().resolve() / ".test-discovery-runtime",
        driver=driver,
        endpoint_lock_factory=endpoint_factory,
    )
    return runtime, driver, resources


def test_owner_resources_publish_worker_and_presence_without_native_start() -> None:
    runtime, driver, resources = _runtime_resources()
    started = runtime.start()
    assert started["state"] == RuntimeState.DEGRADED
    assert started["degradedReasons"] == ("PRODUCT_INTEGRATION_UNVERIFIED",)
    health = runtime.health_snapshot()
    assert health["workerAlive"] is True
    assert health["peerCount"] == 2
    assert health["listenerReady"] is False
    assert health["connectedPeerCount"] == 0
    assert health["readyPeerCount"] == 0
    assert health["productIntegrationVerified"] is False
    diagnostic = resources.cached_diagnostic()
    assert diagnostic["operationCounts"]["hello"] == 1
    assert all(
        diagnostic["operationCounts"][operation] == 0
        for operation in {"start", "snapshot_nodes", "listen", "connect"}
    )

    runtime.stop(time.monotonic() + 3)
    final = driver.health_snapshot()
    assert final["state"] == "STOPPED"
    assert final["workerAlive"] is False
    assert final["peerCount"] == 0
    stopped_diagnostic = resources.cached_diagnostic()
    assert stopped_diagnostic["activeThreadCount"] == 0
    assert stopped_diagnostic["operationCounts"]["stop"] == 1


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("startup-error", "WORKER_START_FAILED"),
        ("malformed-readiness", "WORKER_PROTOCOL_ERROR"),
    ],
)
def test_owner_resources_keep_host_lifecycle_degraded_on_worker_start_failure(
    mode: str, reason: str
) -> None:
    runtime, driver, resources = _runtime_resources(mode=mode)
    started = runtime.start()
    assert started["state"] == RuntimeState.DEGRADED
    assert started["degradedReasons"] == (reason,)
    health = runtime.health_snapshot()
    assert health["workerAlive"] is False
    assert health["peerCount"] == 0
    assert resources.cached_diagnostic()["activeThreadCount"] == 0
    runtime.stop(time.monotonic() + 2)
    assert driver.health_snapshot()["state"] == "STOPPED"
