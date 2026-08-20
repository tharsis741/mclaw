# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Strict local Device Manifest loading and public-document freezing.

The local YAML is a product-owned input.  It is read once after the Runtime
owns its endpoint and before the isolated Worker starts.  ``bindings`` never
leave the process; the public document is created only after the Worker has
returned a verified local device identity.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import Any, Mapping, NoReturn

import yaml
from yaml.events import AliasEvent
from yaml.nodes import MappingNode


LOCAL_MANIFEST_PATH = Path("/data/local/tmp/.mclaw/dsoftbus/device.yaml")
MANIFEST_SCHEMA = "mclaw.device-manifest/v1"
LOCAL_MANIFEST_MAX = 65_536
PUBLIC_MANIFEST_MAX = 24_576

_MAX_INT64 = 2**63 - 1
_RESOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
_CAPABILITY = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,63}$")
_DEVICE_ID = re.compile(r"^urn:mclaw:device:oh:[0-9a-f]{64}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_RFC3339_UTC = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z$"
)


class ManifestError(RuntimeError):
    """Stable, non-sensitive Manifest validation failure."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _fail(code: str, detail: str = "") -> NoReturn:
    raise ManifestError(code, detail)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return copy.deepcopy(value)


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return copy.deepcopy(value)


def _exact_object(value: Any, keys: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail("MANIFEST_SCHEMA_INVALID", f"{label} must be an object")
    actual = frozenset(value)
    if actual != keys:
        _fail(
            "MANIFEST_SCHEMA_INVALID",
            f"{label} keys mismatch missing={sorted(keys - actual)} "
            f"extra={sorted(actual - keys)}",
        )
    return value


def _text(value: Any, label: str, *, maximum: int = 128) -> str:
    if not isinstance(value, str):
        _fail("MANIFEST_SCHEMA_INVALID", f"{label} must be a string")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise ManifestError(
            "MANIFEST_SCHEMA_INVALID", f"{label} is not valid UTF-8"
        ) from error
    if not 1 <= size <= maximum or "\x00" in value:
        _fail(
            "MANIFEST_SCHEMA_INVALID",
            f"{label} UTF-8 length must be in 1..{maximum}",
        )
    return value


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail(
            "MANIFEST_SCHEMA_INVALID",
            f"{label} must be an integer in {minimum}..{maximum}",
        )
    return value


def _timestamp(value: Any, label: str) -> str:
    text = _text(value, label, maximum=40)
    if _RFC3339_UTC.fullmatch(text) is None:
        _fail("MANIFEST_SCHEMA_INVALID", f"{label} must be RFC3339 UTC with Z")
    try:
        datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise ManifestError(
            "MANIFEST_SCHEMA_INVALID", f"{label} is not a real UTC timestamp"
        ) from error
    return text


class _StrictManifestLoader(yaml.SafeLoader):
    """SafeLoader variant that rejects aliases, merges and duplicate keys."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(AliasEvent):
            _fail("MANIFEST_YAML_INVALID", "YAML aliases are forbidden")
        return super().compose_node(parent, index)

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[str, Any]:
        if not isinstance(node, MappingNode):
            _fail("MANIFEST_YAML_INVALID", "mapping node required")
        result: dict[str, Any] = {}
        for key_node, value_node in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge" or key_node.value == "<<":
                _fail("MANIFEST_YAML_INVALID", "YAML merge keys are forbidden")
            key = self.construct_object(key_node, deep=deep)
            if type(key) is not str:
                _fail("MANIFEST_YAML_INVALID", "mapping keys must be strings")
            if key in result:
                _fail("MANIFEST_YAML_INVALID", f"duplicate mapping key {key!r}")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _parse_yaml(raw: bytes) -> dict[str, Any]:
    if not isinstance(raw, bytes) or not raw or len(raw) > LOCAL_MANIFEST_MAX:
        _fail("MANIFEST_FILE_INVALID", "YAML byte length is outside the allowed range")
    if raw.startswith(b"\xef\xbb\xbf") or b"\x00" in raw:
        _fail("MANIFEST_YAML_INVALID", "BOM and NUL are forbidden")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ManifestError("MANIFEST_YAML_INVALID", "YAML must be UTF-8") from error
    try:
        value = yaml.load(text, Loader=_StrictManifestLoader)
    except ManifestError:
        raise
    except yaml.YAMLError as error:
        raise ManifestError("MANIFEST_YAML_INVALID", "invalid strict YAML") from error
    if not isinstance(value, dict):
        _fail("MANIFEST_SCHEMA_INVALID", "Manifest root must be an object")
    return value


def _validate_device(value: Any) -> None:
    device = _exact_object(
        value,
        frozenset({"manufacturer", "model", "displayName", "os"}),
        "device",
    )
    _text(device["manufacturer"], "device.manufacturer")
    _text(device["model"], "device.model")
    _text(device["displayName"], "device.displayName")
    os_value = _exact_object(
        device["os"],
        frozenset({"name", "version", "apiLevel", "arch"}),
        "device.os",
    )
    _text(os_value["name"], "device.os.name")
    _text(os_value["version"], "device.os.version")
    _integer(os_value["apiLevel"], "device.os.apiLevel", 1, 2**31 - 1)
    _text(os_value["arch"], "device.os.arch")


def _validate_resources(value: Any) -> set[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 128:
        _fail("MANIFEST_SCHEMA_INVALID", "resources must contain 1..128 entries")
    resource_ids: set[str] = set()
    for index, item in enumerate(value):
        resource = _exact_object(
            item,
            frozenset(
                {
                    "resourceId",
                    "type",
                    "name",
                    "capabilities",
                    "operations",
                }
            ),
            f"resources[{index}]",
        )
        resource_id = _text(
            resource["resourceId"], f"resources[{index}].resourceId"
        )
        if _RESOURCE_ID.fullmatch(resource_id) is None or resource_id in resource_ids:
            _fail(
                "MANIFEST_SCHEMA_INVALID",
                f"resources[{index}].resourceId is invalid or duplicated",
            )
        resource_ids.add(resource_id)
        _text(resource["type"], f"resources[{index}].type")
        _text(resource["name"], f"resources[{index}].name")
        capabilities = resource["capabilities"]
        if not isinstance(capabilities, list) or not 1 <= len(capabilities) <= 32:
            _fail(
                "MANIFEST_SCHEMA_INVALID",
                f"resources[{index}].capabilities must contain 1..32 entries",
            )
        normalized: list[str] = []
        for cap_index, capability in enumerate(capabilities):
            text = _text(
                capability,
                f"resources[{index}].capabilities[{cap_index}]",
                maximum=64,
            )
            if _CAPABILITY.fullmatch(text) is None:
                _fail(
                    "MANIFEST_SCHEMA_INVALID",
                    f"resources[{index}].capabilities[{cap_index}] is invalid",
                )
            normalized.append(text)
        if len(normalized) != len(set(normalized)):
            _fail(
                "MANIFEST_SCHEMA_INVALID",
                f"resources[{index}].capabilities contains duplicates",
            )
        if resource["operations"] != ["read"]:
            _fail(
                "MANIFEST_SCHEMA_INVALID",
                f"resources[{index}].operations must be exactly ['read']",
            )
    return resource_ids


def _validate_bindings(value: Any, resource_ids: set[str]) -> None:
    if not isinstance(value, dict):
        _fail("MANIFEST_SCHEMA_INVALID", "bindings must be an object")
    if not set(value).issubset(resource_ids):
        _fail("MANIFEST_SCHEMA_INVALID", "bindings must be a resources subset")
    for resource_id, item in value.items():
        if resource_id != "host.system":
            _fail("MANIFEST_SCHEMA_INVALID", "only host.system may configure a reader")
        binding = _exact_object(
            item, frozenset({"reader", "config"}), f"bindings.{resource_id}"
        )
        if binding["reader"] != "system" or binding["config"] != {}:
            _fail(
                "MANIFEST_SCHEMA_INVALID",
                "host.system binding must be system with empty config",
            )


def _validate_local_document(value: dict[str, Any]) -> None:
    manifest = _exact_object(
        value,
        frozenset(
            {
                "schemaVersion",
                "revision",
                "generatedAt",
                "device",
                "resources",
                "bindings",
            }
        ),
        "manifest",
    )
    if manifest["schemaVersion"] != MANIFEST_SCHEMA:
        _fail("MANIFEST_SCHEMA_INVALID", "schemaVersion mismatch")
    _integer(manifest["revision"], "revision", 1, _MAX_INT64)
    _timestamp(manifest["generatedAt"], "generatedAt")
    _validate_device(manifest["device"])
    resource_ids = _validate_resources(manifest["resources"])
    _validate_bindings(manifest["bindings"], resource_ids)


@dataclass(frozen=True, slots=True)
class LocalManifestTemplate:
    """Deeply immutable private product template read during startup."""

    document: Mapping[str, Any]
    source_byte_length: int
    source_sha256: str

    @property
    def revision(self) -> int:
        return int(self.document["revision"])


def parse_local_manifest_template(raw: bytes) -> LocalManifestTemplate:
    """Parse already-read YAML bytes; useful for deterministic host tests."""

    value = _parse_yaml(raw)
    _validate_local_document(value)
    return LocalManifestTemplate(
        document=_freeze(value),
        source_byte_length=len(raw),
        source_sha256=hashlib.sha256(raw).hexdigest(),
    )


def encode_local_manifest(document: Mapping[str, Any]) -> bytes:
    """Encode a validated device-local Manifest without YAML ambiguity."""

    if not isinstance(document, Mapping):
        raise TypeError("document must be a mapping")
    value = _thaw(document)
    if not isinstance(value, dict):
        raise TypeError("document must be a mapping")
    _validate_local_document(value)
    try:
        raw = yaml.safe_dump(
            value,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
            width=4_096,
        ).encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError, yaml.YAMLError) as error:
        raise ManifestError(
            "MANIFEST_YAML_INVALID", "Manifest cannot be encoded as strict YAML"
        ) from error
    parsed = parse_local_manifest_template(raw)
    if _thaw(parsed.document) != value:
        _fail("MANIFEST_YAML_INVALID", "Manifest YAML round trip changed the document")
    return raw


def _stable_stat_key(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def load_local_manifest_template(
    path: str | os.PathLike[str] = LOCAL_MANIFEST_PATH,
    *,
    expected_uid: int = 0,
) -> LocalManifestTemplate:
    """No-follow, bounded, stable read of the product-owned local YAML."""

    if type(expected_uid) is not int or expected_uid < 0:
        raise ValueError("expected_uid must be a non-negative integer")
    manifest_path = Path(path)
    try:
        before_open = os.stat(manifest_path, follow_symlinks=False)
    except OSError as error:
        raise ManifestError("MANIFEST_FILE_INVALID", "Manifest is not readable") from error
    if stat.S_ISLNK(before_open.st_mode):
        _fail("MANIFEST_FILE_INVALID", "Manifest symlink is forbidden")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(manifest_path, flags)
    except OSError as error:
        raise ManifestError("MANIFEST_FILE_INVALID", "no-follow open failed") from error
    try:
        opened = os.fstat(descriptor)
        mode = stat.S_IMODE(opened.st_mode)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != expected_uid
            or mode & 0o022
            or opened.st_size < 1
            or opened.st_size > LOCAL_MANIFEST_MAX
            or (before_open.st_dev, before_open.st_ino)
            != (opened.st_dev, opened.st_ino)
        ):
            _fail("MANIFEST_FILE_INVALID", "owner, mode, type or size is invalid")
        chunks: list[bytes] = []
        remaining = LOCAL_MANIFEST_MAX + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 16_384))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after_read = os.fstat(descriptor)
        if (
            len(raw) > LOCAL_MANIFEST_MAX
            or len(raw) != opened.st_size
            or _stable_stat_key(opened) != _stable_stat_key(after_read)
        ):
            _fail("MANIFEST_FILE_CHANGED", "Manifest changed during read")
    finally:
        os.close(descriptor)
    return parse_local_manifest_template(raw)


@dataclass(frozen=True, slots=True)
class ManifestDescriptor:
    schema_version: str
    revision: int
    digest: str

    def as_mapping(self) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "schemaVersion": self.schema_version,
                "revision": self.revision,
                "digest": self.digest,
            }
        )


def validate_manifest_descriptor(value: Any) -> ManifestDescriptor:
    descriptor = _exact_object(
        value, frozenset({"schemaVersion", "revision", "digest"}), "descriptor"
    )
    if descriptor["schemaVersion"] != MANIFEST_SCHEMA:
        _fail("MANIFEST_DESCRIPTOR_INVALID", "schemaVersion mismatch")
    revision = _integer(descriptor["revision"], "descriptor.revision", 1, _MAX_INT64)
    digest = descriptor["digest"]
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        _fail("MANIFEST_DESCRIPTOR_INVALID", "digest is invalid")
    return ManifestDescriptor(MANIFEST_SCHEMA, revision, digest)


def _compact_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError) as error:
        raise ManifestError(
            "MANIFEST_SCHEMA_INVALID", "Manifest is not finite JSON"
        ) from error


def _validate_public_document(value: dict[str, Any]) -> None:
    manifest = _exact_object(
        value,
        frozenset(
            {
                "schemaVersion",
                "deviceId",
                "revision",
                "generatedAt",
                "device",
                "resources",
                "digest",
            }
        ),
        "publicManifest",
    )
    if manifest["schemaVersion"] != MANIFEST_SCHEMA:
        _fail("MANIFEST_SCHEMA_INVALID", "schemaVersion mismatch")
    if not isinstance(manifest["deviceId"], str) or _DEVICE_ID.fullmatch(
        manifest["deviceId"]
    ) is None:
        _fail("MANIFEST_SCHEMA_INVALID", "deviceId is invalid")
    _integer(manifest["revision"], "revision", 1, _MAX_INT64)
    _timestamp(manifest["generatedAt"], "generatedAt")
    _validate_device(manifest["device"])
    _validate_resources(manifest["resources"])
    digest = manifest["digest"]
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        _fail("MANIFEST_SCHEMA_INVALID", "digest is invalid")
    preimage = dict(manifest)
    del preimage["digest"]
    expected = f"sha256:{hashlib.sha256(_compact_json(preimage)).hexdigest()}"
    if digest != expected:
        _fail("MANIFEST_DIGEST_MISMATCH", "public Manifest digest mismatch")


@dataclass(frozen=True, slots=True)
class PublicManifest:
    document: Mapping[str, Any]
    descriptor: ManifestDescriptor
    canonical_bytes: bytes


def build_public_manifest(
    template: LocalManifestTemplate, device_id: str
) -> PublicManifest:
    """Build and freeze the network document after verified identity exists."""

    if not isinstance(template, LocalManifestTemplate):
        raise TypeError("template must be LocalManifestTemplate")
    if not isinstance(device_id, str) or _DEVICE_ID.fullmatch(device_id) is None:
        _fail("MANIFEST_SCHEMA_INVALID", "deviceId is invalid")
    local = _thaw(template.document)
    local.pop("bindings", None)
    local["deviceId"] = device_id
    digest = f"sha256:{hashlib.sha256(_compact_json(local)).hexdigest()}"
    local["digest"] = digest
    _validate_public_document(local)
    encoded = _compact_json(local)
    if len(encoded) > PUBLIC_MANIFEST_MAX:
        _fail("MANIFEST_TOO_LARGE", "public Manifest exceeds byte limit")
    descriptor = ManifestDescriptor(MANIFEST_SCHEMA, template.revision, digest)
    return PublicManifest(
        document=_freeze(local),
        descriptor=descriptor,
        canonical_bytes=encoded,
    )


def validate_public_manifest(value: Any) -> PublicManifest:
    if not isinstance(value, dict):
        _fail("MANIFEST_SCHEMA_INVALID", "public Manifest must be an object")
    document = copy.deepcopy(value)
    _validate_public_document(document)
    encoded = _compact_json(document)
    if len(encoded) > PUBLIC_MANIFEST_MAX:
        _fail("MANIFEST_TOO_LARGE", "public Manifest exceeds byte limit")
    descriptor = ManifestDescriptor(
        MANIFEST_SCHEMA, document["revision"], document["digest"]
    )
    return PublicManifest(_freeze(document), descriptor, encoded)


__all__ = [
    "LOCAL_MANIFEST_MAX",
    "LOCAL_MANIFEST_PATH",
    "MANIFEST_SCHEMA",
    "ManifestDescriptor",
    "ManifestError",
    "PUBLIC_MANIFEST_MAX",
    "LocalManifestTemplate",
    "PublicManifest",
    "build_public_manifest",
    "encode_local_manifest",
    "load_local_manifest_template",
    "parse_local_manifest_template",
    "validate_manifest_descriptor",
    "validate_public_manifest",
]
