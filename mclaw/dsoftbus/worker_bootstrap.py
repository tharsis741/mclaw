#!/usr/bin/env python3
# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Minimal isolated bootstrap for the OpenHarmony DSoftBus worker.

This file deliberately uses only the Python standard library.  It verifies
the content-addressed worker source tree before adding that tree to
``sys.path`` and importing :mod:`mclaw.dsoftbus.worker`.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
from typing import Any, NoReturn


WORKER_CODE_MANIFEST_FILENAME = "worker-code-manifest.json"
WORKER_CODE_MANIFEST_SCHEMA = "mclaw.dsoftbus.worker-code-manifest"
WORKER_CODE_MANIFEST_BYTES_MAX = 65_536
WORKER_CODE_FILE_MAX = 32
WORKER_BOOTSTRAP_RELATIVE_PATH = "mclaw/dsoftbus/worker_bootstrap.py"
_HEX64 = frozenset("0123456789abcdef")


class BootstrapError(RuntimeError):
    """A stable bootstrap validation failure."""


def _fail(detail: str) -> NoReturn:
    raise BootstrapError(detail)


def _canonical_bytes(value: Any) -> bytes:
    try:
        rendered = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise BootstrapError("manifest contains a non-canonical value") from error
    return (rendered + "\n").encode("utf-8")


def _reject_constant(_: str) -> NoReturn:
    _fail("manifest contains a non-finite number")


def _pairs_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("manifest contains a duplicate key")
        result[key] = value
    return result


def _strict_object(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > WORKER_CODE_MANIFEST_BYTES_MAX:
        _fail("manifest byte length is outside the allowed range")
    if raw.startswith(b"\xef\xbb\xbf"):
        _fail("manifest UTF-8 BOM is forbidden")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_pairs_object,
            parse_constant=_reject_constant,
        )
    except BootstrapError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise BootstrapError("manifest is not valid UTF-8 JSON") from error
    if not isinstance(value, dict) or _canonical_bytes(value) != raw:
        _fail("manifest is not canonical JSON plus one LF")
    return value


def _exact_keys(value: Any, expected: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or frozenset(value) != expected:
        _fail(f"{label} key set mismatch")
    return value


def _is_hex64(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _HEX64 for character in value)
    )


def _read_regular_no_follow(path: Path, *, maximum: int, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise BootstrapError(f"cannot open {label}") from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size < 0
            or before.st_size > maximum
        ):
            _fail(f"{label} is not an allowed regular file")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
        )
        if identity_before != identity_after or len(raw) != before.st_size:
            _fail(f"{label} changed while being read")
        return raw
    finally:
        os.close(descriptor)


def _safe_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        _fail("worker code path is invalid")
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or str(parsed) != value or any(
        part in {"", ".", ".."} for part in parsed.parts
    ):
        _fail("worker code path is not canonical relative POSIX")
    if len(value.encode("utf-8")) > 1_024:
        _fail("worker code path is too long")
    return value


def _assert_no_link_components(root: Path, candidate: Path) -> None:
    try:
        relative = candidate.relative_to(root)
    except ValueError as error:
        raise BootstrapError("worker code path escapes package root") from error
    current = root
    root_status = current.lstat()
    if not stat.S_ISDIR(root_status.st_mode) or stat.S_ISLNK(root_status.st_mode):
        _fail("package root is not a real directory")
    for component in relative.parts[:-1]:
        current /= component
        status = current.lstat()
        if not stat.S_ISDIR(status.st_mode) or stat.S_ISLNK(status.st_mode):
            _fail("worker code path traverses a link or non-directory")


def _tree_digest(files: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for record in files:
        digest.update(record["path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(record["byteLength"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(record["sha256"].encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def verify_worker_code_bundle(
    *,
    bootstrap_path: Path,
    manifest_path: Path,
    expected_manifest_sha256: str,
) -> Path:
    """Verify the exact code bundle and return its package root."""

    if not _is_hex64(expected_manifest_sha256):
        _fail("expected manifest SHA-256 is invalid")
    bootstrap_absolute = bootstrap_path.absolute()
    if bootstrap_absolute.is_symlink() or not bootstrap_absolute.is_file():
        _fail("bootstrap is not a direct regular file")
    if Path(os.path.realpath(bootstrap_absolute)) != bootstrap_absolute:
        _fail("bootstrap path contains link indirection")
    if len(bootstrap_absolute.parents) < 4:
        _fail("bootstrap path is outside the fixed package layout")
    package_root = bootstrap_absolute.parents[2]
    release_root = package_root.parent
    if package_root.name != "site" or not _is_hex64(release_root.name):
        _fail("worker package is not in a content-addressed release")
    expected_manifest_path = release_root / WORKER_CODE_MANIFEST_FILENAME
    if manifest_path.absolute() != expected_manifest_path:
        _fail("worker manifest path differs from the release layout")

    manifest_raw = _read_regular_no_follow(
        expected_manifest_path,
        maximum=WORKER_CODE_MANIFEST_BYTES_MAX,
        label="worker code manifest",
    )
    if hashlib.sha256(manifest_raw).hexdigest() != expected_manifest_sha256:
        _fail("worker code manifest SHA-256 mismatch")
    manifest = _exact_keys(
        _strict_object(manifest_raw),
        frozenset({"bootstrap", "files", "packageRoot", "schema", "treeSha256"}),
        "worker code manifest",
    )
    if (
        manifest["schema"] != WORKER_CODE_MANIFEST_SCHEMA
        or manifest["packageRoot"] != "site"
        or manifest["bootstrap"] != WORKER_BOOTSTRAP_RELATIVE_PATH
        or not _is_hex64(manifest["treeSha256"])
        or manifest["treeSha256"] != release_root.name
    ):
        _fail("worker code manifest identity mismatch")
    if not isinstance(manifest["files"], list) or not (
        1 <= len(manifest["files"]) <= WORKER_CODE_FILE_MAX
    ):
        _fail("worker code manifest file count is invalid")

    checked: list[dict[str, Any]] = []
    previous = ""
    for index, untrusted in enumerate(manifest["files"]):
        record = _exact_keys(
            untrusted,
            frozenset({"byteLength", "path", "sha256"}),
            f"worker code file {index}",
        )
        relative = _safe_relative_path(record["path"])
        if previous and relative <= previous:
            _fail("worker code files are not strictly path-sorted")
        previous = relative
        if (
            type(record["byteLength"]) is not int
            or record["byteLength"] < 1
            or record["byteLength"] > 1_048_576
            or not _is_hex64(record["sha256"])
        ):
            _fail("worker code file metadata is invalid")
        file_path = package_root.joinpath(*PurePosixPath(relative).parts)
        _assert_no_link_components(package_root, file_path)
        raw = _read_regular_no_follow(
            file_path,
            maximum=record["byteLength"],
            label="worker code file",
        )
        if (
            len(raw) != record["byteLength"]
            or hashlib.sha256(raw).hexdigest() != record["sha256"]
        ):
            _fail("worker code file bytes mismatch")
        checked.append(dict(record))
    if not any(
        record["path"] == WORKER_BOOTSTRAP_RELATIVE_PATH for record in checked
    ):
        _fail("worker manifest does not contain its bootstrap")
    if _tree_digest(checked) != manifest["treeSha256"]:
        _fail("worker module tree SHA-256 mismatch")
    return package_root


def main() -> int:
    if sys.flags.isolated != 1 or sys.flags.no_site != 1:
        sys.stderr.write("mclaw-dsoftbus-worker-bootstrap: ISOLATION_REQUIRED\n")
        return 78
    manifest_value = os.environ.get("MCLAW_DSOFTBUS_WORKER_MANIFEST", "")
    expected_hash = os.environ.get(
        "MCLAW_DSOFTBUS_WORKER_MANIFEST_SHA256", ""
    )
    try:
        package_root = verify_worker_code_bundle(
            bootstrap_path=Path(__file__),
            manifest_path=Path(manifest_value),
            expected_manifest_sha256=expected_hash,
        )
    except (BootstrapError, OSError):
        sys.stderr.write("mclaw-dsoftbus-worker-bootstrap: BUNDLE_INVALID\n")
        return 78
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(package_root))
    try:
        worker = importlib.import_module("mclaw.dsoftbus.worker")
        return int(worker.main())
    except Exception:
        sys.stderr.write("mclaw-dsoftbus-worker-bootstrap: WORKER_FAILED\n")
        return 70


if __name__ == "__main__":
    exit_code = main()
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    finally:
        os._exit(exit_code)

