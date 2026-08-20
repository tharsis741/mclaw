# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath

import pytest

from mclaw.dsoftbus import baseline, manifest, protocol, runtime_profile
from mclaw.dsoftbus.product import (
    ProductActivationError,
    ProductRuntimeInputs,
    create_product_runtime,
)
from mclaw.dsoftbus.worker_supervisor import (
    ProfileWorkerLauncher,
    WorkerIdentityExpectation,
)


_RAW_TOKEN_ID = "671314740"
_TOKEN_HASH = "sha256:" + hashlib.sha256(
    b"mclaw-dsoftbus-token-id\0" + _RAW_TOKEN_ID.encode("ascii")
).hexdigest()

_BOARD_PARAMETERS = {
    "const.ohos.fullname": "OpenHarmony-6.1.0.31",
    "const.ohos.version": "KaihongOS 6.1.0.04",
    "const.ohos.apiversion": "23",
    "const.product.software.version": "M-Robots OS 6.1.0.04Stan",
    "const.product.manufacturer": "default",
    "const.product.brand": "Kaihong",
    "const.product.name": "KaihongBoard-3588S",
    "const.product.model": "ohos",
    "const.product.cpu.abilist": "arm64-v8a",
}


def _board_identity(**overrides: str) -> runtime_profile._OhosDeviceIdentity:
    parameters = {**_BOARD_PARAMETERS, **overrides}
    return runtime_profile._device_identity_from_parameters(
        parameters,
        machine="aarch64",
    )


def _portable_manifest_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def write(path: Path, raw: bytes, *, mode: int) -> None:
        path.write_bytes(raw)
        path.chmod(mode)

    monkeypatch.setattr(runtime_profile, "_write_new_file", write)
    monkeypatch.setattr(runtime_profile, "_fsync_directory", lambda _path: None)


def test_device_identity_is_derived_from_board_parameters_without_sample_defaults() -> None:
    identity = _board_identity()

    assert identity.manufacturer == "Kaihong"
    assert identity.model == "KaihongBoard-3588S"
    assert identity.display_name == "KaihongBoard-3588S"
    assert identity.os_name == "KaihongOS"
    assert identity.os_version == "6.1.0.04"
    assert identity.api_level == 23
    assert identity.abi == "arm64-v8a"
    assert identity.machine == "aarch64"

    distinct = _board_identity(
        **{
            "const.product.manufacturer": "Kaihong Digital",
            "const.product.name": "Kaihong BotBook",
            "const.product.model": "KHP-LC802",
        }
    )
    assert distinct.manufacturer == "Kaihong Digital"
    assert distinct.model == "KHP-LC802"
    assert distinct.display_name == "Kaihong BotBook"


@pytest.mark.parametrize(
    ("overrides", "machine", "code"),
    [
        (
            {"const.product.brand": "", "const.product.manufacturer": "default"},
            "aarch64",
            "DEVICE_IDENTITY_UNAVAILABLE",
        ),
        (
            {"const.product.name": "", "const.product.model": "ohos"},
            "aarch64",
            "DEVICE_IDENTITY_UNAVAILABLE",
        ),
        (
            {"const.ohos.apiversion": "22"},
            "aarch64",
            "OH61_RUNTIME_UNSUPPORTED",
        ),
        (
            {"const.product.cpu.abilist": "armeabi-v7a"},
            "aarch64",
            "OH61_RUNTIME_UNSUPPORTED",
        ),
        ({}, "x86_64", "OH61_RUNTIME_UNSUPPORTED"),
    ],
)
def test_device_identity_fails_closed_on_missing_or_unsupported_facts(
    overrides: dict[str, str], machine: str, code: str
) -> None:
    with pytest.raises(runtime_profile.RuntimeProfileError) as error:
        runtime_profile._device_identity_from_parameters(
            {**_BOARD_PARAMETERS, **overrides},
            machine=machine,
        )
    assert error.value.code == code


def test_device_manifest_is_generated_refreshed_and_not_rewritten_when_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _portable_manifest_writer(monkeypatch)

    path = runtime_profile._ensure_manifest(tmp_path, _board_identity())
    first_raw = path.read_bytes()
    first = manifest.parse_local_manifest_template(first_raw)
    assert first.revision == 1
    assert first.document["device"] == {
        "manufacturer": "Kaihong",
        "model": "KaihongBoard-3588S",
        "displayName": "KaihongBoard-3588S",
        "os": {
            "name": "KaihongOS",
            "version": "6.1.0.04",
            "apiLevel": 23,
            "arch": "aarch64",
        },
    }

    assert runtime_profile._ensure_manifest(tmp_path, _board_identity()) == path
    assert path.read_bytes() == first_raw

    changed = _board_identity(
        **{
            "const.product.name": "Kaihong BotBook",
            "const.product.model": "KHP-LC802",
        }
    )
    runtime_profile._ensure_manifest(tmp_path, changed)
    refreshed = manifest.parse_local_manifest_template(path.read_bytes())
    assert refreshed.revision == 2
    assert refreshed.document["device"]["model"] == "KHP-LC802"
    assert refreshed.document["device"]["displayName"] == "Kaihong BotBook"


def test_generated_manifest_safely_round_trips_parameter_punctuation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _portable_manifest_writer(monkeypatch)
    identity = _board_identity(
        **{
            "const.product.name": "研发板: #1",
            "const.product.model": "ohos",
        }
    )

    path = runtime_profile._ensure_manifest(tmp_path, identity)
    loaded = manifest.parse_local_manifest_template(path.read_bytes())

    assert loaded.document["device"]["model"] == "研发板: #1"
    assert loaded.document["device"]["displayName"] == "研发板: #1"


def test_generated_manifest_refuses_invalid_existing_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _portable_manifest_writer(monkeypatch)
    path = tmp_path / "device.yaml"
    path.write_text("not-a-manifest\n", encoding="utf-8")

    with pytest.raises(runtime_profile.RuntimeProfileError) as error:
        runtime_profile._ensure_manifest(tmp_path, _board_identity())
    assert error.value.code == "MANIFEST_INSTALL_INVALID"


def _artifact(path: str, salt: str) -> dict[str, object]:
    return {
        "byteLength": len(salt.encode("utf-8")) + 1,
        "gid": 0,
        "mode": "0555",
        "path": path,
        "resolvedPath": path,
        "sha256": hashlib.sha256(salt.encode("utf-8")).hexdigest(),
        "uid": 0,
    }


def _tree_hash(records: list[dict[str, object]]) -> str:
    digest = hashlib.sha256()
    for record in records:
        digest.update(str(record["path"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(record["byteLength"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(record["sha256"]).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _document() -> dict[str, object]:
    source_records = [
        {
            "byteLength": index + 10,
            "path": relative,
            "sha256": hashlib.sha256(relative.encode("utf-8")).hexdigest(),
        }
        for index, relative in enumerate(sorted(baseline.WORKER_MODULE_RELATIVE_PATHS))
    ]
    tree_sha = _tree_hash(source_records)
    release = f"/data/local/tmp/.mclaw/dsoftbus/worker/releases/{tree_sha}"
    worker_files: list[dict[str, object]] = []
    for source in source_records:
        relative = str(source["path"])
        path = str(PurePosixPath(release) / "site" / PurePosixPath(relative))
        worker_files.append(
            {
                **_artifact(path, relative),
                "byteLength": source["byteLength"],
                "mode": "0400",
                "relativePath": relative,
                "sha256": source["sha256"],
            }
        )
    bootstrap = next(
        record
        for record in worker_files
        if record["relativePath"] == baseline.WORKER_BOOTSTRAP_RELATIVE_PATH
    )
    softbus = _artifact(baseline.REMOTE_SOFTBUS_LIBRARY, "softbus")
    target = {
        "abi": "arm64-v8a",
        "apiLevel": 23,
        "capabilitySet": [],
        "gid": 0,
        "machine": "aarch64",
        "pythonVersion": "3.12.7",
        "selinuxContext": "u:r:su:s0",
        "supplementaryGids": [1006, 1007, 2000, 3009],
        "uid": 0,
        "versions": {
            "const.ohos.fullname": "OpenHarmony-6.1.0.31",
            "const.ohos.version": "KaihongOS 6.1.0.04",
            "const.product.software.version": "M-Robots OS 6.1.0.04Stan",
        },
    }
    closure = {
        "identity": {
            "capabilitySet": [],
            "gid": 0,
            "launcher": _artifact(baseline.TOKEN_LAUNCHER, "launcher"),
            "permission": baseline.DISTRIBUTED_DATASYNC_PERMISSION,
            "permissionGranted": True,
            "processName": "mclaw",
            "publicTokenIdAvailable": False,
            "sealedTokenIdHash": _TOKEN_HASH,
            "selinuxContext": "u:r:su:s0",
            "supplementaryGids": [1006, 1007, 2000, 3009],
            "uid": 0,
        },
        "libraries": {
            "bundledLibcxx": _artifact(baseline.BUNDLED_LIBCXX_LIBRARY, "bundled"),
            "cjBindFfi": _artifact(baseline.CJ_BIND_FFI_LIBRARY, "cj-ffi"),
            "cjBindNative": _artifact(baseline.CJ_BIND_NATIVE_LIBRARY, "cj-native"),
            "deviceManagerFfi": _artifact(
                baseline.DEVICE_MANAGER_FFI_LIBRARY, "device-manager"
            ),
            "permission": _artifact(baseline.PERMISSION_LIBRARY, "permission"),
            "releaseLibcxx": _artifact(baseline.RELEASE_LIBCXX_LIBRARY, "release"),
            "shim": _artifact(baseline.SHIM_LIBRARY, "shim"),
            "systemLibcxx": _artifact(baseline.SYSTEM_LIBCXX_LIBRARY, "system"),
        },
        "python": {
            "deployment": {
                "files": worker_files,
                "manifest": {
                    **_artifact(f"{release}/worker-code-manifest.json", "manifest"),
                    "mode": "0400",
                },
                "moduleTreeSha256": tree_sha,
                "workerBootstrapFile": bootstrap["path"],
                "workerBootstrapSha256": bootstrap["sha256"],
            },
            "dynamicLibpython": _artifact(
                baseline.PYTHON_DYNAMIC_LIBRARY, "libpython"
            ),
            "executable": _artifact(baseline.PYTHON_EXECUTABLE, "python"),
            "releaseLibraryDirs": list(baseline.PYTHON_RELEASE_LIBRARY_DIRS),
        },
        "softbus": softbus,
        "softbusSocketCap": 16,
    }
    fingerprint_source = {
        "runtimeClosure": closure,
        "softbus": {
            "library": softbus,
            "requiredExports": list(baseline.REQUIRED_SOFTBUS_EXPORTS),
            "transportAcl": {
                "actions": "create,open",
                "applicationType": "native_app",
                "artifact": _artifact(
                    runtime_profile.SOFTBUS_TRANSPORT_ACL.as_posix(), "transport-acl"
                ),
                "deviceIdType": "NETWORKID",
                "packageName": "mclaw",
                "regexp": False,
                "securityLevel": "public",
                "sessionNames": ["mclaw.a2a.v1", "mclaw.a2a.v1.client"],
                "uid": 0,
            },
        },
        "target": target,
    }
    return {
        "generatedAt": "2026-08-17T00:00:00Z",
        **fingerprint_source,
        "schema": baseline.RUNTIME_PROFILE_SCHEMA,
        "systemFingerprint": "sha256:"
        + hashlib.sha256(protocol.canonical_json_bytes(fingerprint_source)).hexdigest(),
    }


def _write_profile(root: Path, document: dict[str, object] | None = None) -> Path:
    path = root / baseline.RUNTIME_PROFILE_FILENAME
    path.write_bytes(protocol.canonical_json_bytes(_document() if document is None else document))
    return path


def test_local_profile_is_canonical_internal_state_without_raw_identity(
    tmp_path: Path,
) -> None:
    path = _write_profile(tmp_path)
    loaded = baseline.load_runtime_profile(path)
    raw = path.read_text(encoding="utf-8")

    assert isinstance(loaded, baseline.RuntimeProfile)
    assert loaded.document["schema"] == baseline.RUNTIME_PROFILE_SCHEMA
    assert _RAW_TOKEN_ID not in raw
    assert "bootId" not in raw
    assert "networkId" not in raw
    assert "descriptor" not in raw.casefold()


def test_local_profile_rejects_fingerprint_or_unknown_fields(tmp_path: Path) -> None:
    fingerprint = _document()
    fingerprint["systemFingerprint"] = "sha256:" + "0" * 64
    with pytest.raises(baseline.BaselineError, match="fingerprint"):
        baseline.load_runtime_profile(_write_profile(tmp_path, fingerprint))

    unknown = _document()
    unknown["descriptor"] = "forbidden"
    with pytest.raises(baseline.BaselineError, match="extra"):
        baseline.load_runtime_profile(_write_profile(tmp_path, unknown))


@pytest.mark.skipif(os.name == "nt", reason="product argv uses POSIX absolute paths")
def test_local_profile_launches_worker_with_live_boot_and_memory_only_token(
    tmp_path: Path,
) -> None:
    profile = baseline.load_runtime_profile(_write_profile(tmp_path))
    boot_id = "9f41d64b-7db0-4ed1-a837-c82e0b30f3fb"

    launcher = ProfileWorkerLauncher(
        profile=profile,
        raw_token_id=_RAW_TOKEN_ID,
        expected_boot_id=boot_id,
    )
    identity = WorkerIdentityExpectation.from_profile(profile)

    assert launcher._argv[1:3] == ("--expected-boot-id", boot_id)
    assert launcher._argv[3:6] == ("--exec", "--token-id", _RAW_TOKEN_ID)
    assert launcher._environment["MCLAW_DSOFTBUS_PROFILE"] == str(profile.path)
    assert identity.token_id_hash == _TOKEN_HASH
    assert identity.socket_cap == 16


def test_product_inputs_are_derived_from_prepared_local_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = baseline.load_runtime_profile(_write_profile(tmp_path))
    prepared = runtime_profile.PreparedRuntimeProfile(
        profile=profile,
        raw_token_id=_RAW_TOKEN_ID,
        current_boot_id="9f41d64b-7db0-4ed1-a837-c82e0b30f3fb",
    )
    monkeypatch.setattr(runtime_profile, "prepare_runtime_profile", lambda _root: prepared)

    inputs = ProductRuntimeInputs.from_local_state(tmp_path)

    assert inputs.ready is True
    assert inputs.profile_path == str(profile.path)
    assert inputs.profile_sha256 == profile.sha256
    assert inputs.raw_token_id == _RAW_TOKEN_ID
    assert inputs.socket_cap == 16


def test_required_activation_failure_is_a_non_native_product_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        ProductRuntimeInputs,
        "from_local_state",
        classmethod(lambda _cls, _root: ProductRuntimeInputs(status_code="MCLAW_TOKEN_MISSING")),
    )

    with pytest.raises(ProductActivationError) as error:
        create_product_runtime(
            provider_runtime=object(),
            config={"dsoftbus": {"enabled": "auto"}},
            workspace=tmp_path,
            state_root=tmp_path / "state",
            require_activation=True,
        )

    assert error.value.code == "MCLAW_TOKEN_MISSING"
    assert runtime_profile.activation_error_message(error.value.code) == "未找到 M-Claw 系统身份"
