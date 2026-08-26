# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
from typing import Any
import uuid

import pytest

from mclaw.dsoftbus.discovery_resources import DiscoveryOwnerResources
from mclaw.dsoftbus.manifest import parse_local_manifest_template
from mclaw.dsoftbus.presence import derive_public_device_id
from mclaw.dsoftbus.publication import freeze_local_publications
from mclaw.dsoftbus.worker_supervisor import (
    SubprocessWorkerLauncher,
    WorkerIdentityExpectation,
    WorkerSupervisor,
    WorkerSupervisorError,
)


_FAKE_WORKER = (
    Path(__file__).parent / "fixtures" / "dsoftbus" / "fake_worker_target.py"
).resolve()
_TOKEN_HASH = "sha256:" + "a" * 64
_RUNTIME_ID = "00000000-0000-4000-8000-000000000101"
_LOCAL_YAML = b"""schemaVersion: mclaw.device-manifest/v1
revision: 7
generatedAt: "2026-07-31T00:00:00Z"
device:
  manufacturer: Kaihong
  model: M-Robots
  displayName: Lab robot
  os:
    name: KaihongOS
    version: "6.1.0.04"
    apiLevel: 23
    arch: aarch64
resources:
  - resourceId: host.system
    type: system
    name: Host system
    capabilities: [status]
    operations: [read]
bindings:
  host.system:
    reader: system
    config: {}
"""


def _encoded_hello() -> str:
    value = {
        "identity": {
            "capabilitySet": ["CAP_NET_RAW"],
            "distributedDataSyncGranted": True,
            "gid": 0,
            "selinuxDomain": "u:r:su:s0",
            "supplementaryGids": [1006, 1007],
            "tokenIdHash": _TOKEN_HASH,
            "uid": 0,
        },
        "localUdid": "local-device",
        "nativeAbiVersion": 1,
        "socketCap": 16,
    }
    raw = (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode()
    return base64.b64encode(raw).decode("ascii")


def _supervisor(*, mode: str = "phase-b") -> WorkerSupervisor:
    epoch = str(uuid.uuid4())
    launcher = SubprocessWorkerLauncher(
        argv=(
            sys.executable,
            str(_FAKE_WORKER),
            "--epoch",
            epoch,
            "--hello-json-base64",
            _encoded_hello(),
            "--mode",
            mode,
        ),
        environment=dict(os.environ),
        start_time_reader=lambda pid: pid + 20_000,
    )
    return WorkerSupervisor(
        launcher=launcher,
        identity=WorkerIdentityExpectation(
            uid=0,
            gid=0,
            supplementary_gids=(1006, 1007),
            capability_set=("CAP_NET_RAW",),
            token_id_hash=_TOKEN_HASH,
            selinux_domain="u:r:su:s0",
            socket_cap=16,
        ),
        control_timeout=1.0,
    )


def test_supervisor_cannot_open_native_operations_before_publication_gate() -> None:
    supervisor = _supervisor()
    verified = supervisor.start()
    with pytest.raises(WorkerSupervisorError) as early:
        supervisor.start_node_events()
    assert early.value.code == "WORKER_NOT_READY"
    assert supervisor.diagnostic_snapshot()["operationCounts"]["start"] == 0

    template = parse_local_manifest_template(_LOCAL_YAML)
    publications = freeze_local_publications(
        template=template,
        verified_device_id=verified.public_device_id,
        verified_agent_id=verified.public_agent_id,
        runtime_instance_id=_RUNTIME_ID,
    )
    wrong = dict(publications.gate_mapping())
    wrong["deviceId"] = "urn:mclaw:device:oh:" + "f" * 64
    with pytest.raises(WorkerSupervisorError) as mismatch:
        supervisor.complete_manifest_phase_b(wrong)
    assert mismatch.value.code == "WORKER_IDENTITY_MISMATCH"
    assert supervisor.diagnostic_snapshot()["operationCounts"]["start"] == 0
    supervisor.stop(time.monotonic() + 2)


def test_supervisor_typed_phase_b_operations_and_event_handoff() -> None:
    supervisor = _supervisor(mode="phase-b-event")
    verified = supervisor.start()
    publications = freeze_local_publications(
        template=parse_local_manifest_template(_LOCAL_YAML),
        verified_device_id=verified.public_device_id,
        verified_agent_id=verified.public_agent_id,
        runtime_instance_id=_RUNTIME_ID,
    )
    supervisor.complete_manifest_phase_b(publications.gate_mapping())
    supervisor.start_node_events()
    event = supervisor.wait_event(time.monotonic() + 1)
    assert event is not None and event["event"] == "node-online"
    page = supervisor.snapshot_nodes_page()
    assert page["snapshotId"] == "00000000-0000-4000-8000-000000000002"
    assert supervisor.get_node_udid("peer-network") == "peer-b"
    supervisor.start_device_discovery()
    discovery = supervisor.stop_device_discovery()
    assert [dict(item) for item in discovery["devices"]] == [
        {
            "deviceIdSha256": "e" * 64,
            "deviceName": "Candidate device",
            "deviceTypeId": 533,
            "networkIdSha256": "",
            "publicDeviceId": "",
        }
    ]
    assert discovery["failureNativeCode"] is None
    assert discovery["stopped"] is True
    assert dict(supervisor.begin_device_bind("e" * 64)) == {
        "binding": True,
        "deviceIdSha256": "e" * 64,
    }
    assert dict(supervisor.get_device_bind_status("e" * 64)) == {
        "deviceIdSha256": "e" * 64,
        "nativeCode": 0,
        "status": "bound",
    }
    assert [dict(item) for item in supervisor.list_trusted_devices()] == [
        {
            "deviceIdSha256": "e" * 64,
            "deviceName": "Peer device",
            "deviceTypeId": 1,
            "networkId": "peer-network",
        }
    ]
    assert dict(supervisor.unbind_device("peer-network")) == {
        "deviceIdSha256": "e" * 64,
        "unbound": True,
    }
    assert supervisor.list_trusted_devices() == ()
    assert supervisor.listen() == 10
    connection = supervisor.connect("peer-network")
    assert dict(connection) == {"mtu": 32768, "socket": 11}
    assert supervisor.send_bytes(11, b"hello") == 5
    supervisor.close_socket(11)
    diagnostic = supervisor.diagnostic_snapshot()
    assert diagnostic["manifestPhaseBComplete"] is True
    assert diagnostic["nativeNodeEventsStarted"] is True
    assert diagnostic["operationCounts"] == {
        "hello": 1,
        "start": 1,
        "snapshot_nodes": 1,
        "get_node_udid": 1,
        "start_device_discovery": 1,
        "stop_device_discovery": 1,
        "begin_device_bind": 1,
        "get_device_bind_status": 1,
        "list_trusted_devices": 2,
        "unbind_device": 1,
        "listen": 1,
        "connect": 1,
        "send_bytes": 1,
        "close_socket": 1,
        "stop": 0,
    }
    supervisor.stop(time.monotonic() + 2)


def test_supervisor_preserves_native_discovery_failure_details() -> None:
    supervisor = _supervisor(mode="phase-b-device-discovery-error")
    verified = supervisor.start()
    publications = freeze_local_publications(
        template=parse_local_manifest_template(_LOCAL_YAML),
        verified_device_id=verified.public_device_id,
        verified_agent_id=verified.public_agent_id,
        runtime_instance_id=_RUNTIME_ID,
    )
    supervisor.complete_manifest_phase_b(publications.gate_mapping())
    supervisor.start_node_events()
    supervisor.start_device_discovery()

    with pytest.raises(WorkerSupervisorError) as caught:
        supervisor.stop_device_discovery()

    assert caught.value.code == "NATIVE_ERROR"
    assert caught.value.native_code == -902
    assert caught.value.phase == "stop_device_discovery"
    supervisor.stop(time.monotonic() + 2)


def test_supervisor_preserves_partial_discovery_native_code() -> None:
    supervisor = _supervisor(mode="phase-b-device-discovery-partial")
    verified = supervisor.start()
    publications = freeze_local_publications(
        template=parse_local_manifest_template(_LOCAL_YAML),
        verified_device_id=verified.public_device_id,
        verified_agent_id=verified.public_agent_id,
        runtime_instance_id=_RUNTIME_ID,
    )
    supervisor.complete_manifest_phase_b(publications.gate_mapping())
    supervisor.start_node_events()
    supervisor.start_device_discovery()

    discovery = supervisor.stop_device_discovery()

    assert len(discovery["devices"]) == 1
    assert discovery["failureNativeCode"] == -903
    supervisor.stop(time.monotonic() + 2)


def test_supervisor_counts_one_terminal_event_overflow_per_epoch() -> None:
    supervisor = _supervisor(mode="phase-b-overflow")
    verified = supervisor.start()
    publications = freeze_local_publications(
        template=parse_local_manifest_template(_LOCAL_YAML),
        verified_device_id=verified.public_device_id,
        verified_agent_id=verified.public_agent_id,
        runtime_instance_id=_RUNTIME_ID,
    )
    supervisor.complete_manifest_phase_b(publications.gate_mapping())
    assert supervisor.health_updates()["eventOverflowCount"] == 0
    try:
        supervisor.start_node_events()
    except WorkerSupervisorError as error:
        assert error.code in {
            "PARENT_EVENT_CAPACITY_FATAL",
            "WORKER_PROTOCOL_ERROR",
        }
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        health = supervisor.health_updates()
        if not health["workerAlive"]:
            break
        time.sleep(0.01)
    else:
        raise AssertionError("overflow did not terminate the worker epoch")
    assert supervisor.health_updates()["eventOverflowCount"] == 1
    supervisor.stop(time.monotonic() + 2)
    assert supervisor.health_updates()["eventOverflowCount"] == 1


def test_discovery_phase_b_freezes_then_snapshots_listens_and_connects() -> None:
    async def lifecycle() -> tuple[
        dict[str, Any], dict[str, Any], tuple[Any, ...], dict[str, Any]
    ]:
        resources = DiscoveryOwnerResources(
            supervisor=_supervisor(),
            manifest_template=parse_local_manifest_template(_LOCAL_YAML),
        )
        started = dict(await resources.start(_RUNTIME_ID))
        publication = dict(resources.publication_snapshot())
        peers = resources.cached_public_peers()
        stopped = dict(await resources.stop(lambda: time.monotonic() + 2))
        return started, publication, peers, stopped

    started, publication, peers, stopped = asyncio.run(lifecycle())
    assert started["state"] == "READY"
    assert started["degradedReasons"] == ()
    health = started["healthUpdates"]
    assert health["workerAlive"] is True
    assert health["listenerReady"] is True
    assert health["listenerSocketCount"] == 1
    assert health["connectedPeerCount"] == 1
    assert health["productIntegrationVerified"] is True
    assert publication == {
        "agentCardGenerationCount": 1,
        "listenerGenerationCount": 1,
        "manifestDescriptorGenerationCount": 1,
        "publicManifestGenerationCount": 1,
        "manifestPhaseBComplete": True,
        "stateEpochFrozen": True,
    }
    assert len(peers) == 1
    assert peers[0]["connectionState"] == "OPEN"
    assert peers[0]["agentAvailability"] == "BINDING"
    assert "networkId" not in peers[0]
    assert "udid" not in peers[0]
    assert stopped["workerAlive"] is False
    assert stopped["openSocketCount"] == 0


def test_discovery_unbind_closes_route_removes_presence_and_confirms_absence() -> None:
    async def lifecycle() -> tuple[Any, ...]:
        resources = DiscoveryOwnerResources(
            supervisor=_supervisor(),
            manifest_template=parse_local_manifest_template(_LOCAL_YAML),
        )
        started = dict(await resources.start(_RUNTIME_ID))
        before = tuple(dict(item) for item in resources.list_trusted_devices())
        result = dict(await resources.unbind_device("d" * 64))
        after = tuple(dict(item) for item in resources.list_trusted_devices())
        peers = resources.cached_public_peers()
        diagnostic = dict(resources.cached_diagnostic())
        stopped = dict(await resources.stop(lambda: time.monotonic() + 2))
        return started, before, result, after, peers, diagnostic, stopped

    started, before, result, after, peers, diagnostic, stopped = asyncio.run(
        lifecycle()
    )
    assert started["state"] == "READY"
    assert before == (
        {
            "deviceIdSha256": "d" * 64,
            "deviceName": "Peer device",
            "deviceTypeId": 1,
            "online": True,
            "publicDeviceId": derive_public_device_id("peer-b"),
        },
    )
    assert result == {
        "deviceIdSha256": "d" * 64,
        "publicDeviceId": before[0]["publicDeviceId"],
        "unbound": True,
    }
    assert after == ()
    assert peers == ()
    assert diagnostic["operationCounts"]["list_trusted_devices"] == 6
    assert diagnostic["operationCounts"]["unbind_device"] == 1
    assert stopped["workerAlive"] is False


def test_product_pairing_discovers_binds_and_only_unpairs_mclaw_owned_acl(
    tmp_path: Path,
) -> None:
    async def lifecycle() -> tuple[Any, ...]:
        ownership_path = tmp_path / "dsoftbus" / "paired-devices.json"
        resources = DiscoveryOwnerResources(
            supervisor=_supervisor(),
            manifest_template=parse_local_manifest_template(_LOCAL_YAML),
            pairing_state_path=ownership_path,
            discovery_window_s=0,
        )
        started = dict(await resources.start(_RUNTIME_ID))
        external_before = tuple(dict(item) for item in resources.list_trusted_devices())
        discovery = await resources.discover_devices()
        assert discovery["failureNativeCode"] is None
        discovered = tuple(dict(item) for item in discovery["devices"])
        paired = dict(await resources.pair_device("e" * 64))
        managed_after = tuple(dict(item) for item in resources.list_trusted_devices())
        paired_discovery = tuple(
            dict(item) for item in (await resources.discover_devices())["devices"]
        )
        unpaired = dict(await resources.unbind_device("e" * 64))
        managed_final = tuple(dict(item) for item in resources.list_trusted_devices())
        unpaired_discovery = tuple(
            dict(item) for item in (await resources.discover_devices())["devices"]
        )
        stopped = dict(await resources.stop(lambda: time.monotonic() + 2))
        return (
            started,
            external_before,
            discovered,
            paired,
            managed_after,
            paired_discovery,
            unpaired,
            managed_final,
            unpaired_discovery,
            ownership_path.read_text(encoding="utf-8"),
            stopped,
        )

    (
        started,
        external_before,
        discovered,
        paired,
        managed_after,
        paired_discovery,
        unpaired,
        managed_final,
        unpaired_discovery,
        ownership,
        stopped,
    ) = asyncio.run(lifecycle())
    assert started["state"] == "READY"
    assert external_before == ()
    assert discovered == (
        {
            "deviceIdSha256": "e" * 64,
            "deviceName": "Candidate device",
            "deviceTypeId": 533,
            "publicDeviceId": "",
        },
    )
    assert paired == {
        "bound": True,
        "deviceIdSha256": "e" * 64,
        "nativeCode": 0,
        "status": "bound",
    }
    assert managed_after[0]["deviceIdSha256"] == "e" * 64
    assert paired_discovery == ()
    assert unpaired["deviceIdSha256"] == "e" * 64
    assert unpaired["unbound"] is True
    assert managed_final == ()
    assert unpaired_discovery == discovered
    assert '"deviceIdSha256":[]' in ownership
    assert stopped["workerAlive"] is False


def test_discovery_unbind_does_not_report_success_before_trust_state_converges() -> None:
    async def lifecycle() -> None:
        resources = DiscoveryOwnerResources(
            supervisor=_supervisor(mode="phase-b-unbind-unconfirmed"),
            manifest_template=parse_local_manifest_template(_LOCAL_YAML),
            unbind_confirm_timeout_s=0.01,
            unbind_poll_interval_s=0.001,
        )
        await resources.start(_RUNTIME_ID)
        with pytest.raises(WorkerSupervisorError, match="DEVICE_UNBIND_UNCONFIRMED"):
            await resources.unbind_device("d" * 64)
        assert tuple(resources.list_trusted_devices()) != ()
        await resources.stop(lambda: time.monotonic() + 2)

    asyncio.run(lifecycle())


def test_product_discovery_correlates_online_softbus_identity() -> None:
    async def lifecycle() -> tuple[dict[str, Any], dict[str, Any]]:
        resources = DiscoveryOwnerResources(
            supervisor=_supervisor(mode="phase-b-connected-candidate"),
            manifest_template=parse_local_manifest_template(_LOCAL_YAML),
            discovery_window_s=0,
        )
        started = dict(await resources.start(_RUNTIME_ID))
        discovery = dict(await resources.discover_devices())
        await resources.stop(lambda: time.monotonic() + 2)
        return started, discovery

    started, discovery = asyncio.run(lifecycle())
    assert started["state"] == "READY"
    assert discovery == {
        "devices": (
            {
                "deviceIdSha256": "e" * 64,
                "deviceName": "Candidate device",
                "deviceTypeId": 533,
                "publicDeviceId": derive_public_device_id("peer-b"),
            },
        ),
        "failureNativeCode": None,
    }


def test_product_pairing_retains_pending_ownership_after_unconfirmed_success(
    tmp_path: Path,
) -> None:
    async def lifecycle() -> tuple[str, bool, str]:
        ownership_path = tmp_path / "dsoftbus" / "paired-devices.json"
        resources = DiscoveryOwnerResources(
            supervisor=_supervisor(mode="phase-b-bind-unconfirmed"),
            manifest_template=parse_local_manifest_template(_LOCAL_YAML),
            pairing_state_path=ownership_path,
            discovery_window_s=0,
            bind_confirm_timeout_s=0.01,
            bind_poll_interval_s=0.005,
        )
        await resources.start(_RUNTIME_ID)
        await resources.discover_devices()
        try:
            await resources.pair_device("e" * 64)
        except WorkerSupervisorError as error:
            code = error.code
            outcome_unknown = error.outcome_unknown
        else:
            raise AssertionError("unconfirmed bind was accepted")
        await resources.stop(lambda: time.monotonic() + 2)
        return code, outcome_unknown, ownership_path.read_text(encoding="utf-8")

    code, outcome_unknown, ownership = asyncio.run(lifecycle())
    assert code == "DEVICE_BIND_UNCONFIRMED"
    assert outcome_unknown is True
    assert f'"deviceIdSha256":["{"e" * 64}"]' in ownership


def test_discovery_rebuilds_native_handles_after_worker_epoch_death() -> None:
    class FreshLauncher:
        def spawn(self) -> Any:
            return SubprocessWorkerLauncher(
                argv=(
                    sys.executable,
                    str(_FAKE_WORKER),
                    "--epoch",
                    str(uuid.uuid4()),
                    "--hello-json-base64",
                    _encoded_hello(),
                    "--mode",
                    "phase-b",
                ),
                environment=dict(os.environ),
                start_time_reader=lambda pid: pid + 20_000,
            ).spawn()

    async def lifecycle() -> tuple[
        dict[str, Any],
        dict[str, Any],
        tuple[tuple[str, tuple[str, ...]], ...],
    ]:
        supervisor = WorkerSupervisor(
            launcher=FreshLauncher(),
            identity=WorkerIdentityExpectation(
                uid=0,
                gid=0,
                supplementary_gids=(1006, 1007),
                capability_set=("CAP_NET_RAW",),
                token_id_hash=_TOKEN_HASH,
                selinux_domain="u:r:su:s0",
                socket_cap=16,
            ),
            control_timeout=1.0,
        )
        resources = DiscoveryOwnerResources(
            supervisor=supervisor,
            manifest_template=parse_local_manifest_template(_LOCAL_YAML),
        )
        started = dict(await resources.start(_RUNTIME_ID))
        transitions: list[tuple[str, tuple[str, ...]]] = []

        def changed(value: Any) -> None:
            state = value.get("_lifecycleState")
            if state is not None:
                transitions.append((state, tuple(value["_degradedReasons"])))

        resources.set_health_change_callback(changed)
        first = dict(supervisor.health_updates())
        try:
            os.kill(first["workerPid"], signal.SIGTERM)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                current = dict(supervisor.health_updates())
                publication = dict(resources.publication_snapshot())
                if (
                    current["workerAlive"]
                    and current["restartCount"] == 1
                    and current["workerPid"] != first["workerPid"]
                    and publication["listenerGenerationCount"] == 2
                ):
                    break
                await asyncio.sleep(0.02)
            else:
                raise AssertionError(
                    "worker recovery did not complete: "
                    f"health={dict(supervisor.health_updates())!r}, "
                    f"publication={dict(resources.publication_snapshot())!r}, "
                    f"transitions={transitions!r}"
                )
            recovered = dict(supervisor.health_updates())
            publication = dict(resources.publication_snapshot())
        finally:
            await resources.stop(lambda: time.monotonic() + 2)
        return recovered, publication, tuple(transitions)

    recovered, publication, transitions = asyncio.run(lifecycle())
    assert recovered["restartCount"] == 1
    assert recovered["workerAlive"] is True
    assert publication["listenerGenerationCount"] == 2
    assert transitions[-2:] == (
        ("DEGRADED", ("WORKER_START_FAILED",)),
        ("READY", ()),
    )


def test_online_socket_close_retries_with_backoff_and_new_generation() -> None:
    async def lifecycle() -> tuple[tuple[str, int], tuple[str, int], int]:
        supervisor = _supervisor()
        resources = DiscoveryOwnerResources(
            supervisor=supervisor,
            manifest_template=parse_local_manifest_template(_LOCAL_YAML),
            reconnect_random=lambda: 0.5,
        )
        await resources.start(_RUNTIME_ID)
        first = dict(resources.cached_public_peers()[0])
        original_connect = supervisor.connect
        attempts = 0

        def fail_once(network_id: str) -> Any:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise WorkerSupervisorError("WORKER_PROTOCOL_ERROR")
            return original_connect(network_id)

        supervisor.connect = fail_once  # type: ignore[method-assign]
        try:
            resources._process_event(  # noqa: SLF001 - owner-loop fault injection
                {
                    "data": {
                        "code": "SOCKET_CLOSED",
                        "nativeCode": 0,
                        "scope": "socket",
                        "socket": 11,
                    },
                    "event": "closed",
                    "workerEpoch": "00000000-0000-4000-8000-000000000001",
                }
            )
            deadline = time.monotonic() + 1.0
            reconnecting: dict[str, Any] | None = None
            while time.monotonic() < deadline:
                rows = resources.cached_public_peers()
                if (
                    attempts == 1
                    and rows
                    and rows[0]["connectionState"] == "RECONNECTING"
                ):
                    reconnecting = dict(rows[0])
                    break
                await asyncio.sleep(0.01)
            assert reconnecting is not None

            deadline = time.monotonic() + 2.0
            recovered: dict[str, Any] | None = None
            while time.monotonic() < deadline:
                rows = resources.cached_public_peers()
                if (
                    rows
                    and rows[0]["connectionState"] == "OPEN"
                    and rows[0].get("connectionGeneration") == 2
                ):
                    recovered = dict(rows[0])
                    break
                await asyncio.sleep(0.01)
            assert recovered is not None
        finally:
            await resources.stop(lambda: time.monotonic() + 2)
        return (
            (str(first["connectionState"]), int(first["connectionGeneration"])),
            (
                str(reconnecting["connectionState"]),
                int(first["connectionGeneration"]),
            ),
            attempts,
        )

    first, reconnecting, attempts = asyncio.run(lifecycle())
    assert first == ("OPEN", 1)
    assert reconnecting == ("RECONNECTING", 1)
    assert attempts == 2


def test_invalid_runtime_identity_keeps_all_post_hello_operations_closed() -> None:
    async def lifecycle() -> tuple[dict[str, Any], dict[str, Any]]:
        resources = DiscoveryOwnerResources(
            supervisor=_supervisor(),
            manifest_template=parse_local_manifest_template(_LOCAL_YAML),
        )
        started = dict(await resources.start("not-a-runtime-id"))
        diagnostic = dict(resources.cached_diagnostic())
        await resources.stop(lambda: time.monotonic() + 2)
        return started, diagnostic

    started, diagnostic = asyncio.run(lifecycle())
    assert started["state"] == "DEGRADED"
    assert started["degradedReasons"] == ("WORKER_START_FAILED",)
    assert diagnostic["operationCounts"]["hello"] == 1
    assert all(
        diagnostic["operationCounts"][operation] == 0
        for operation in {"start", "snapshot_nodes", "listen", "connect"}
    )
    assert diagnostic["workerAlive"] is False
    assert diagnostic["dispatchExecutionCount"] == 0
    assert diagnostic["inflightMessageCount"] == 0
    assert diagnostic["localTurnCount"] == 0
    assert diagnostic["tokenReserved"] == 0


def test_owner_thread_is_stable_for_direct_supervisor_use() -> None:
    """Document that the typed API remains owner-confined."""

    supervisor = _supervisor()
    supervisor.start()
    errors: list[str] = []

    def foreign_thread() -> None:
        try:
            supervisor.start_node_events()
        except WorkerSupervisorError as error:
            errors.append(error.code)

    thread = threading.Thread(target=foreign_thread)
    thread.start()
    thread.join()
    assert errors == ["WORKER_SUPERVISOR_OWNER_MISMATCH"]
    supervisor.stop(time.monotonic() + 2)
