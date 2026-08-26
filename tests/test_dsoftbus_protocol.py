# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import subprocess
import sys
import uuid

import pytest

from mclaw.dsoftbus import protocol


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def test_device_discovery_uses_ten_second_scan_window() -> None:
    assert protocol.DEVICE_DISCOVERY_WINDOW_S == 10.0
    assert protocol.CONTROL_TIMEOUT_S == 5
    assert protocol.DEVICE_MANAGER_OPERATION_TIMEOUT_S == 10.0
    assert protocol.DEVICE_MANAGER_WORKER_TIMEOUT_S == 11.0
    assert (
        protocol.DEVICE_MANAGER_WORKER_TIMEOUT_S
        > protocol.DEVICE_MANAGER_OPERATION_TIMEOUT_S
        > protocol.CONTROL_TIMEOUT_S
    )


def test_canonical_json_and_digest_are_stable() -> None:
    value = {"z": 1, "é": "utf8", "a": [True, None]}
    expected = '{"a":[true,null],"z":1,"é":"utf8"}\n'.encode()
    assert protocol.canonical_json_bytes(value) == expected
    assert protocol.canonical_digest(value) == f"sha256:{_sha(expected)}"


@pytest.mark.parametrize(
    "raw",
    [
        b'{"a":1,"a":2}\n',
        b'{"value":NaN}\n',
        b"\xef\xbb\xbf{}\n",
        b"{}\r\n",
        b"{ }\n",
        b'{"value":"\\ud800"}\n',
    ],
)
def test_strict_canonical_json_rejects_invalid_bytes(raw: bytes) -> None:
    with pytest.raises(protocol.ProtocolError):
        protocol.strict_json_loads(
            raw,
            max_bytes=128,
            require_canonical=True,
            require_object=True,
        )


def test_worker_command_round_trip_and_strict_base64() -> None:
    command_id = "12345678-1234-4234-9234-123456789abc"
    line = protocol.encode_worker_command(
        "send_bytes", {"data": "YWJj", "socket": 7}, command_id=command_id
    )
    parsed = protocol.parse_worker_command(line)
    assert parsed.command_id == command_id
    assert parsed.operation == "send_bytes"
    assert parsed.args == {"data": "YWJj", "socket": 7}
    assert parsed.line_bytes == len(line)


@pytest.mark.parametrize(
    "value",
    [
        {"args": {}, "id": "not-a-uuid", "op": "hello", "v": 1},
        {
            "args": {"socket": True},
            "id": "12345678-1234-4234-9234-123456789abc",
            "op": "close_socket",
            "v": 1,
        },
        {
            "args": {"data": "YQ", "socket": 1},
            "id": "12345678-1234-4234-9234-123456789abc",
            "op": "send_bytes",
            "v": 1,
        },
        {
            "args": {},
            "extra": False,
            "id": "12345678-1234-4234-9234-123456789abc",
            "op": "hello",
            "v": 1,
        },
    ],
)
def test_worker_command_rejects_shape_type_and_encoding(value: object) -> None:
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_worker_command(protocol.canonical_json_bytes(value))


def test_encode_ipc_object_enforces_line_cap() -> None:
    with pytest.raises(protocol.ProtocolError) as error:
        protocol.encode_ipc_object({"data": "x" * protocol.IPC_LINE_MAX})
    assert error.value.code == "FRAME_TOO_LARGE"


def test_worker_command_uuid_generator_produces_uuid4() -> None:
    parsed = protocol.parse_worker_command(protocol.encode_worker_command("hello", {}))
    assert uuid.UUID(parsed.command_id).version == 4


def test_cross_platform_package_import_does_not_load_native_or_ctypes() -> None:
    script = (
        "import sys; import mclaw.dsoftbus; "
        "assert 'ctypes' not in sys.modules; "
        "assert not any(name.startswith('mclaw.dsoftbus.native') for name in sys.modules)"
    )
    completed = subprocess.run(
        [sys.executable, "-B", "-c", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
