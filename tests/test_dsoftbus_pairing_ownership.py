# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mclaw.dsoftbus.pairing_ownership import (
    PAIRING_OWNERSHIP_SCHEMA,
    PairingOwnershipError,
    PairingOwnershipStore,
)


def test_pairing_ownership_is_canonical_durable_and_idempotent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "dsoftbus" / "paired-devices.json"
    first = PairingOwnershipStore(path)
    assert first.load() == ()
    first.add("e" * 64)
    first.add("d" * 64)
    first.add("e" * 64)

    assert path.read_bytes() == (
        json.dumps(
            {
                "deviceIdSha256": ["d" * 64, "e" * 64],
                "schemaVersion": PAIRING_OWNERSHIP_SCHEMA,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    second = PairingOwnershipStore(path)
    assert second.load() == ("d" * 64, "e" * 64)
    assert second.contains("e" * 64) is True
    second.remove("d" * 64)
    assert PairingOwnershipStore(path).load() == ("e" * 64,)


def test_pairing_ownership_rejects_corrupt_unknown_and_noncanonical_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paired-devices.json"
    path.write_text(
        json.dumps(
            {
                "deviceIdSha256": ["e" * 64],
                "schemaVersion": PAIRING_OWNERSHIP_SCHEMA,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    if os.name == "posix":
        path.chmod(0o600)
    with pytest.raises(PairingOwnershipError, match="PAIRING_STATE_INVALID"):
        PairingOwnershipStore(path).load()


def test_pairing_ownership_write_failure_rolls_back_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = PairingOwnershipStore(tmp_path / "paired-devices.json")
    store.load()
    monkeypatch.setattr(
        os,
        "replace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("denied")),
    )
    with pytest.raises(PairingOwnershipError, match="PAIRING_STATE_WRITE_FAILED"):
        store.add("e" * 64)
    assert store.digests() == ()


def test_pairing_ownership_never_removes_an_unowned_digest(tmp_path: Path) -> None:
    store = PairingOwnershipStore(tmp_path / "paired-devices.json")
    store.load()
    with pytest.raises(PairingOwnershipError, match="DEVICE_NOT_MANAGED"):
        store.remove("f" * 64)
