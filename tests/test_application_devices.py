# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio

from mclaw.application.devices import DeviceService
from mclaw.application.sessions import ApplicationError
from mclaw.dsoftbus.device_management import DeviceManagementCoordinator
from mclaw.dsoftbus.runtime import DsoftbusRuntimeError


DIGEST = "a" * 64
DEVICE_ID = "urn:mclaw:device:oh:" + DIGEST
CANDIDATE = {"deviceIdSha256": DIGEST, "deviceName": "另一台开鸿电脑", "deviceTypeId": 12, "publicDeviceId": ""}


class Runtime:
    def __init__(self):
        self.started = 0
        self.stopped = 0
        self.scans = 0
        self.pairs = []
        self.trusted = []
        self.scan_error = None

    def start(self):
        self.started += 1
        return self.health()

    def health(self):
        return {"state": "READY", "providerReady": False}

    def local_device(self):
        return {"deviceId": "urn:mclaw:device:oh:" + "b" * 64, "deviceName": "本机",
                "manufacturer": "Kaihong", "model": "BotBook", "osName": "Kaihong OS",
                "osVersion": "5.0", "apiLevel": 14, "arch": "aarch64"}

    def list_peers(self, **_):
        return []

    def list_trusted_devices(self):
        return self.trusted

    def discover_devices(self):
        self.scans += 1
        if self.scan_error:
            raise self.scan_error
        return {"devices": [CANDIDATE], "failureNativeCode": None}

    def pair_device(self, identifier):
        self.pairs.append(identifier)
        self.trusted = [{**CANDIDATE, "online": False}]
        return {"deviceIdSha256": identifier, "status": "bound", "nativeCode": 0, "bound": True}

    def unbind_device(self, identifier):
        self.trusted = []
        return {"deviceIdSha256": identifier, "unbound": True}

    def begin_shutdown(self):
        pass

    def stop(self, _deadline):
        self.stopped += 1


@pytest_asyncio.fixture
async def devices(tmp_path, monkeypatch):
    monkeypatch.setenv("MCLAW_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("dsoftbus:\n  enabled: auto\nmodel: preserve-me\n", encoding="utf-8")
    monkeypatch.setattr("mclaw.providers.resolver.resolve_provider_runtime_context", lambda **_: None)
    runtime = Runtime()
    platform = SimpleNamespace(kind="kaihong", features=SimpleNamespace(is_enabled=lambda _: True))
    service = DeviceService(tmp_path, runtime_factory=lambda **_: runtime, platform_factory=lambda: platform)
    await service.start()
    await service._task
    yield service, runtime
    await service.close()


@pytest.mark.asyncio
async def test_snapshot_is_read_only_and_service_is_single_owner(devices):
    service, runtime = devices
    await service.start()
    first, second = await asyncio.gather(service.snapshot(), service.snapshot())
    assert first["localDevice"]["deviceName"] == "本机"
    assert first == second
    assert runtime.started == 1
    assert runtime.scans == 0
    assert not first["provider_ready"]


def test_shared_device_snapshot_does_not_scan_or_prompt():
    runtime = Runtime()
    snapshot = DeviceManagementCoordinator(runtime).snapshot()
    assert snapshot.local_device.device_name == "本机"
    assert runtime.scans == 0


@pytest.mark.asyncio
async def test_discover_pair_and_unpair_keep_trust_and_agent_readiness_distinct(devices):
    service, runtime = devices
    result = await service.act("discover", {})
    assert result["operation"]["action"] == "discover"
    await service._task
    assert (await service.snapshot())["pairableDevices"] == [CANDIDATE]
    await service.act("pair", {"device_id": DIGEST, "confirmed": True})
    await service._task
    status = await service.snapshot()
    assert runtime.pairs == [DIGEST]
    assert status["trustedDevices"][0]["online"] is False
    assert status["devices"] == []
    assert status["pairableDevices"] == []
    await service.act("unpair", {"device_id": DIGEST, "confirmed": True})
    await service._task
    assert (await service.snapshot())["trustedDevices"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [{}, {"device_id": DIGEST}, {"device_id": "invalid", "confirmed": True}, {"device_id": DIGEST, "confirmed": "true"}])
async def test_pairing_requires_an_explicit_valid_device_and_confirmation(devices, data):
    service, runtime = devices
    with pytest.raises(ApplicationError, match="确认"):
        await service.act("pair", data)
    assert not runtime.pairs


@pytest.mark.asyncio
async def test_native_failure_is_returned_as_safe_action_result(devices):
    service, runtime = devices
    runtime.scan_error = DsoftbusRuntimeError("DEVICE_DISCOVERY_FAILED")
    await service.act("discover", {})
    await service._task
    status = await service.snapshot()
    assert status["operation"]["status"] == "failed"
    assert status["operation"]["error"]["code"] == "DEVICE_DISCOVERY_FAILED"
    assert status["localDevice"]["deviceName"] == "本机"


@pytest.mark.asyncio
async def test_disabling_stops_native_owner_and_preserves_other_configuration(devices):
    import yaml

    service, runtime = devices
    await service.configure({"enabled": False})
    await service._task
    assert runtime.stopped == 1
    assert (await service.snapshot())["state"] == "disabled"
    config = yaml.safe_load((service.home / "config.yaml").read_text(encoding="utf-8"))
    assert config["model"] == "preserve-me"
    assert config["dsoftbus"]["enabled"] is False


@pytest.mark.asyncio
async def test_windows_does_not_start_native_runtime(tmp_path):
    platform = SimpleNamespace(kind="windows")
    service = DeviceService(tmp_path, platform_factory=lambda: platform,
                            runtime_factory=lambda **_: pytest.fail("Windows loaded native runtime"))
    await service.start()
    await service._task
    status = await service.snapshot()
    assert not status["supported"] and status["state"] == "unsupported"
    with pytest.raises(ApplicationError):
        await service.act("discover", {})
    await service.close()


@pytest.mark.asyncio
async def test_close_during_activation_reaps_the_new_runtime(devices, monkeypatch):
    service, runtime = devices
    await service.close()
    started = asyncio.Event()
    release = asyncio.Event()
    service._closing = False
    service.runtime = None
    service._task = None

    async def activate():
        started.set()
        await release.wait()
        service.runtime = runtime

    monkeypatch.setattr(service, "_activate", activate)
    await service.start()
    await started.wait()
    closing = asyncio.create_task(service.close())
    await asyncio.sleep(0)
    release.set()
    await closing
    assert runtime.stopped == 2
    assert service.runtime is None
