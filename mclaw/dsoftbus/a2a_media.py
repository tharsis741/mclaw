# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""A2A media Parts backed by verified DSoftBus chunk transfers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any

from .task_files import (
    TASK_INPUT_MANIFEST_MEDIA_TYPE,
    TaskFileError,
    TaskInputDescriptor,
    normalize_task_input_manifest,
)


TASK_INPUT_REFERENCE_PREFIX = "softbus://mclaw/task-input/"
TASK_ARTIFACT_REFERENCE_PREFIX = "softbus://mclaw/task-artifact/"

_INPUT_METADATA_KEYS = frozenset(
    {
        "mclaw.inputId",
        "mclaw.relativePath",
        "mclaw.byteLength",
        "mclaw.sha256",
    }
)
_ARTIFACT_METADATA_KEYS = frozenset(
    {
        "mclaw.transferId",
        "mclaw.byteLength",
        "mclaw.sha256",
    }
)


def task_input_reference_part(
    descriptor: TaskInputDescriptor,
) -> Mapping[str, Any]:
    """Build one A2A URL Part without exposing the caller's local path."""

    return MappingProxyType(
        {
            "url": f"{TASK_INPUT_REFERENCE_PREFIX}{descriptor.input_id}",
            "filename": descriptor.filename,
            "mediaType": descriptor.media_type,
            "metadata": MappingProxyType(
                {
                    "mclaw.inputId": descriptor.input_id,
                    "mclaw.relativePath": descriptor.relative_path,
                    "mclaw.byteLength": descriptor.byte_length,
                    "mclaw.sha256": descriptor.sha256,
                }
            ),
        }
    )


def task_input_descriptor_from_part(
    part: Mapping[str, Any],
) -> TaskInputDescriptor:
    """Validate one task-input URL Part and recover its transfer descriptor."""

    if not isinstance(part, Mapping) or not isinstance(part.get("url"), str):
        raise TaskFileError("TASK_INPUT_INVALID")
    metadata = part.get("metadata")
    if not isinstance(metadata, Mapping) or frozenset(metadata) != _INPUT_METADATA_KEYS:
        raise TaskFileError("TASK_INPUT_INVALID")
    descriptor = TaskInputDescriptor.from_wire(
        {
            "inputId": metadata["mclaw.inputId"],
            "relativePath": metadata["mclaw.relativePath"],
            "filename": part.get("filename"),
            "mediaType": part.get("mediaType"),
            "byteLength": metadata["mclaw.byteLength"],
            "sha256": metadata["mclaw.sha256"],
        }
    )
    if part["url"] != f"{TASK_INPUT_REFERENCE_PREFIX}{descriptor.input_id}":
        raise TaskFileError("TASK_INPUT_INVALID")
    return descriptor


def task_input_parts(
    descriptors: Sequence[TaskInputDescriptor],
    source_scopes: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    """Build independent media Parts plus one optional directory-scope Part."""

    parts: list[Mapping[str, Any]] = [
        task_input_reference_part(descriptor) for descriptor in descriptors
    ]
    if source_scopes:
        manifest = normalize_task_input_manifest(
            {"files": [], "sourceScopes": list(source_scopes)}
        )
        parts.append(
            MappingProxyType(
                {
                    "data": manifest,
                    "mediaType": TASK_INPUT_MANIFEST_MEDIA_TYPE,
                }
            )
        )
    return tuple(parts)


def task_input_manifest_from_parts(
    parts: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    """Recover the bounded input manifest from normalized A2A Parts.

    The data-manifest form remains readable for already persisted tasks. New
    messages represent every concrete file as its own A2A URL Part.
    """

    descriptors: list[TaskInputDescriptor] = []
    manifest: Mapping[str, Any] | None = None
    for part in parts:
        if not isinstance(part, Mapping):
            raise TaskFileError("TASK_INPUT_INVALID")
        if "url" in part:
            descriptors.append(task_input_descriptor_from_part(part))
            continue
        if (
            part.get("mediaType") == TASK_INPUT_MANIFEST_MEDIA_TYPE
            and "data" in part
        ):
            if manifest is not None:
                raise TaskFileError("TASK_INPUT_INVALID")
            manifest = normalize_task_input_manifest(part["data"])

    manifest_files = tuple(manifest["files"]) if manifest is not None else ()
    scopes = tuple(manifest["sourceScopes"]) if manifest is not None else ()
    if descriptors and manifest_files:
        raise TaskFileError("TASK_INPUT_INVALID")
    files: Sequence[Mapping[str, Any]] = (
        tuple(descriptor.wire_value() for descriptor in descriptors)
        if descriptors
        else manifest_files
    )
    if not files and not scopes:
        return None
    return normalize_task_input_manifest(
        {"files": list(files), "sourceScopes": list(scopes)}
    )


def task_artifact_reference_part(
    *,
    transfer_id: str,
    filename: str,
    media_type: str,
    byte_length: int,
    sha256: str,
) -> dict[str, Any]:
    """Build one mutable candidate Part for an outgoing Artifact snapshot."""

    return {
        "url": f"{TASK_ARTIFACT_REFERENCE_PREFIX}{transfer_id}",
        "filename": filename,
        "mediaType": media_type,
        "metadata": {
            "mclaw.transferId": transfer_id,
            "mclaw.byteLength": byte_length,
            "mclaw.sha256": sha256,
        },
    }


def task_artifact_reference_metadata(
    part: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Return raw metadata when a Part uses the DSoftBus Artifact URI."""

    url = part.get("url") if isinstance(part, Mapping) else None
    if not isinstance(url, str) or not url.startswith(TASK_ARTIFACT_REFERENCE_PREFIX):
        return None
    metadata = part.get("metadata")
    if (
        not isinstance(metadata, Mapping)
        or frozenset(metadata) != _ARTIFACT_METADATA_KEYS
    ):
        raise ValueError("invalid task Artifact reference metadata")
    return metadata


__all__ = [
    "TASK_ARTIFACT_REFERENCE_PREFIX",
    "TASK_INPUT_REFERENCE_PREFIX",
    "task_artifact_reference_metadata",
    "task_artifact_reference_part",
    "task_input_descriptor_from_part",
    "task_input_manifest_from_parts",
    "task_input_parts",
    "task_input_reference_part",
]
