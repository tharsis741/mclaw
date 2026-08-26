# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Local Agent tools for the active, identity-bound DSoftBus Runtime."""

from __future__ import annotations

import json
import logging
import math
import re
import traceback
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mclaw.channels.outbound_bridge import run_outbound_coroutine
from mclaw.tools.interrupt import get_interrupt_event
from mclaw.tools.registry import registry

from . import protocol
from .active import get_active_runtime
from .runtime import DsoftbusRuntimeError
from .task_artifact import (
    TaskArtifactError,
    artifact_transfer_parts,
    get_task_artifact_collector,
)
from .task_input_request import (
    TaskInputRequestError,
    get_task_input_request_collector,
)
from .task_source import get_task_source_client

logger = logging.getLogger(__name__)

_DEVICE_ID = re.compile(r"^urn:mclaw:device:oh:[0-9a-f]{64}$")
_STABLE_CODES = frozenset(protocol.RPC_ERROR_CODES)
_AGGREGATE_PROVENANCE = {
    "kind": "aggregate",
    "receivedVia": "softbus",
    "source": "mclaw.dsoftbus.runtime",
}


def _plain(value: Any) -> Any:
    """Copy recursively frozen Runtime values into the JSON result boundary."""

    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _encode(value: Mapping[str, Any]) -> str:
    raw = json.dumps(
        _plain(value),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(raw) > protocol.TOOL_RESULT_MAX:
        return json.dumps(
            {
                "_untrustedRemoteData": False,
                "code": "INTERNAL_ERROR",
                "completion_unknown": False,
                "interrupted": False,
                "success": False,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    return raw


def _failure(
    code: str,
    *,
    interrupted: bool = False,
    completion_unknown: bool = False,
    **extra: Any,
) -> str:
    normalized = code if code in _STABLE_CODES else "INTERNAL_ERROR"
    return _encode(
        {
            "success": False,
            "code": normalized,
            "interrupted": interrupted,
            "completion_unknown": completion_unknown,
            "_untrustedRemoteData": False,
            **extra,
        }
    )


def _runtime_or_failure() -> Any | None:
    return get_active_runtime()


def _validate_exact_args(
    args: Any, *, required: frozenset[str], optional: frozenset[str]
) -> Mapping[str, Any]:
    if not isinstance(args, Mapping):
        raise DsoftbusRuntimeError("INVALID_PARAMS")
    keys = frozenset(args)
    if not required.issubset(keys) or not keys.issubset(required | optional):
        raise DsoftbusRuntimeError("INVALID_PARAMS")
    return args


def _device_id(value: Any) -> str:
    if not isinstance(value, str) or _DEVICE_ID.fullmatch(value) is None:
        raise DsoftbusRuntimeError("INVALID_PARAMS")
    return value


def _canonical_uuid(value: Any, *, optional: bool) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise DsoftbusRuntimeError("INVALID_PARAMS")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as error:
        raise DsoftbusRuntimeError("INVALID_PARAMS") from error
    if parsed.version != 4 or str(parsed) != value:
        raise DsoftbusRuntimeError("INVALID_PARAMS")
    return value


def _handle_error(error: Exception, **extra: Any) -> str:
    if getattr(error, "termination_fence", None) is not None:
        raise error
    code = getattr(error, "code", "INTERNAL_ERROR")
    if code == "INTERNAL_ERROR" or code not in _STABLE_CODES:
        traces: list[str] = []
        current: BaseException | None = error
        visited: set[int] = set()
        while current is not None and id(current) not in visited and len(traces) < 4:
            visited.add(id(current))
            frames = traceback.extract_tb(current.__traceback__)
            location = " <- ".join(
                f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
                for frame in frames[-8:]
            )
            traces.append(f"{type(current).__name__}@{location or 'unavailable'}")
            current = current.__cause__ or current.__context__
        logger.error(
            "DSoftBus tool failure mapped to INTERNAL_ERROR "
            "trace_chain=%s",
            " | caused_by=".join(traces),
        )
    return _failure(
        code,
        interrupted=bool(getattr(error, "interrupted", False)),
        completion_unknown=bool(getattr(error, "outcome_unknown", False)),
        **extra,
    )


async def _capture_outbound(operation: Any) -> tuple[bool, Any]:
    try:
        return True, await operation
    except Exception as error:
        if getattr(error, "termination_fence", None) is not None:
            raise
        return False, error


def _cancel_event(parent_agent: Any) -> Any:
    event = get_interrupt_event()
    if event is not None:
        return event
    current = getattr(parent_agent, "current_turn_cancel_event", None)
    return current() if callable(current) else None


def _run_runtime_outbound(
    operation: Any,
    *,
    loop: Any,
    timeout: float,
    label: str,
    parent_agent: Any,
) -> tuple[bool, Any]:
    bridged = run_outbound_coroutine(
        _capture_outbound(operation),
        loop=loop,
        timeout=timeout,
        platform="dsoftbus",
        display_name="DSoftBus",
        label=label,
        cancel_event=_cancel_event(parent_agent),
        parent_agent=parent_agent,
    )
    if isinstance(bridged, tuple) and len(bridged) == 2:
        return bool(bridged[0]), bridged[1]
    interrupted = bool(getattr(bridged, "interrupted", False))
    completion_unknown = bool(
        getattr(bridged, "completion_unknown", False)
    )
    error_text = str(getattr(bridged, "error", ""))
    code = (
        "AGENT_INTERRUPTED"
        if interrupted
        else "DEADLINE_EXCEEDED"
        if "timed out" in error_text.lower()
        else "INTERNAL_ERROR"
    )
    return False, DsoftbusRuntimeError(
        code,
        outcome_unknown=completion_unknown,
        interrupted=interrupted,
    )


def dsoftbus_tools_available(config: dict | None = None) -> bool:
    """Schemas are visible only after the interactive entrypoint installs Runtime."""

    return get_active_runtime() is not None


def diagnose_dsoftbus_tools(config: dict | None = None) -> dict[str, Any]:
    """Return a local, non-sensitive Registry diagnostic."""

    runtime = get_active_runtime()
    if runtime is None:
        return {
            "available": False,
            "fix": "Start interactive M-Claw on a supported OpenHarmony device.",
            "reason": "runtime-not-active",
        }
    try:
        snapshot = runtime.diagnostic_snapshot()
        state = str(snapshot["lifecycle"]["state"])
        resource = snapshot.get("resource", {})
        input_code = str(resource.get("productInputCode", ""))
        reason = f"runtime-{state.lower()}"
        if input_code:
            reason += f":{input_code}"
        return {
            "available": state not in {"STOPPING", "STOPPED"},
            "fix": (
                "Restart M-Claw with the pinned DSoftBus launch inputs."
                if input_code
                else ""
            ),
            "reason": reason,
        }
    except Exception:
        return {
            "available": False,
            "fix": "Restart M-Claw and inspect local DSoftBus health.",
            "reason": "runtime-diagnostic-unavailable",
        }


def list_peers_handler(args: Mapping[str, Any], **kwargs: Any) -> str:
    try:
        values = _validate_exact_args(
            args, required=frozenset(), optional=frozenset({"ready_only"})
        )
        ready_only = values.get("ready_only", False)
        if type(ready_only) is not bool:
            raise DsoftbusRuntimeError("INVALID_PARAMS")
        runtime = _runtime_or_failure()
        if runtime is None:
            raise DsoftbusRuntimeError("PEER_NOT_READY")
        peers = runtime.list_peers(ready_only=ready_only)
        return _encode(
            {
                "success": True,
                "peers": peers,
                "_mclawProvenance": dict(_AGGREGATE_PROVENANCE),
                "_untrustedRemoteData": bool(peers),
            }
        )
    except Exception as error:
        return _handle_error(error)


async def get_device_context_handler(
    args: Mapping[str, Any], **kwargs: Any
) -> str:
    device_id = ""
    try:
        values = _validate_exact_args(
            args,
            required=frozenset({"device_id"}),
            optional=frozenset({"refresh_state"}),
        )
        device_id = _device_id(values["device_id"])
        refresh = values.get("refresh_state", False)
        if type(refresh) is not bool:
            raise DsoftbusRuntimeError("INVALID_PARAMS")
        runtime = _runtime_or_failure()
        if runtime is None:
            raise DsoftbusRuntimeError("PEER_NOT_READY")
        if refresh:
            prepare = getattr(runtime, "prepare_device_context_refresh", None)
            if callable(prepare):
                loop, operation = prepare(device_id)
                succeeded, value = _run_runtime_outbound(
                    operation,
                    loop=loop,
                    timeout=float(protocol.CONTROL_TIMEOUT_S),
                    label="device-context",
                    parent_agent=kwargs.get("parent_agent"),
                )
                if not succeeded:
                    raise value
                result = value
            else:
                result = await runtime.aget_device_context(
                    device_id,
                    refresh_state=True,
                )
        else:
            result = runtime.get_cached_device_context(device_id)
        if not isinstance(result, Mapping):
            raise DsoftbusRuntimeError("INTERNAL_ERROR")
        return _encode(dict(result))
    except Exception as error:
        return _handle_error(error, device_id=device_id)


def _resolve_task_input_paths(
    raw_input_paths: Any,
    parent_agent: Any,
) -> tuple[str, ...]:
    if not isinstance(raw_input_paths, (tuple, list)) or len(
        raw_input_paths
    ) > protocol.TASK_INPUT_PATH_MAX:
        raise DsoftbusRuntimeError("INVALID_PARAMS")
    if not raw_input_paths:
        return ()
    from mclaw.runtime.manager import RuntimeManager
    from mclaw.tools.file_tools import _resolve_agent_path

    path_policy = RuntimeManager.current().paths
    result: list[str] = []
    for raw_path in raw_input_paths:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise DsoftbusRuntimeError("INVALID_PARAMS")
        resolved = Path(_resolve_agent_path(raw_path, parent_agent)).expanduser()
        decision = path_policy.check("read", resolved)
        if (
            not decision.allowed
            or path_policy.is_runtime_internal_path(decision.resolved)
        ):
            raise DsoftbusRuntimeError("SOURCE_PATH_FORBIDDEN")
        result.append(str(decision.resolved))
    return tuple(result)


async def run_agent_task_handler(args: Mapping[str, Any], **kwargs: Any) -> str:
    device_id = ""
    context_id: str | None = None
    message_id: str | None = None
    try:
        values = _validate_exact_args(
            args,
            required=frozenset({"device_id", "text"}),
            optional=frozenset({"context_id", "message_id", "input_paths"}),
        )
        device_id = _device_id(values["device_id"])
        text = values["text"]
        if not isinstance(text, str) or not 1 <= len(text.encode("utf-8")) <= 24_576:
            raise DsoftbusRuntimeError("INVALID_PARAMS")
        context_id = _canonical_uuid(values.get("context_id"), optional=True)
        message_id = _canonical_uuid(values.get("message_id"), optional=True)
        if message_id is None:
            message_id = str(uuid.uuid4())
        runtime = _runtime_or_failure()
        if runtime is None:
            raise DsoftbusRuntimeError("PEER_NOT_READY")
        parent_agent = kwargs.get("parent_agent")
        input_paths = _resolve_task_input_paths(
            values.get("input_paths", []),
            parent_agent,
        )

        def _event_sink(event: Mapping[str, Any]) -> None:
            emit = getattr(parent_agent, "_emit_event", None)
            if callable(emit):
                emit(_plain(event))

        prepare = getattr(runtime, "prepare_run_agent_task_outbound", None)
        task_options: dict[str, Any] = {
            "context_id": context_id,
            "message_id": message_id,
            "event_sink": _event_sink,
        }
        if input_paths:
            task_options["input_paths"] = input_paths
        if callable(prepare):
            loop, operation, context_id, message_id = prepare(
                device_id,
                text,
                **task_options,
            )
            succeeded, value = _run_runtime_outbound(
                operation,
                loop=loop,
                timeout=math.inf,
                label="agent-task",
                parent_agent=parent_agent,
            )
            if not succeeded:
                raise value
            result = value
        else:
            result = await runtime.arun_agent_task(
                device_id,
                text,
                **task_options,
            )
        if not isinstance(result, Mapping):
            raise DsoftbusRuntimeError("INTERNAL_ERROR")
        return _encode(dict(result))
    except Exception as error:
        return _handle_error(
            error,
            device_id=device_id,
            context_id=context_id,
            message_id=message_id,
        )


def _artifact_display_name(value: Any) -> str:
    if not isinstance(value, str):
        raise TaskArtifactError("INVALID_PARAMS")
    name = Path(value.replace("\\", "/")).name.strip()
    if not name or name in {".", ".."} or len(name.encode("utf-8")) > 160:
        raise TaskArtifactError("INVALID_PARAMS")
    return name


def return_artifact_handler(args: Mapping[str, Any], **kwargs: Any) -> str:
    """Attach JSON or one bounded readable local file to the current Task."""

    try:
        values = _validate_exact_args(
            args,
            required=frozenset({"name"}),
            optional=frozenset({"data", "path", "description", "media_type"}),
        )
        choices = frozenset({"data", "path"}) & frozenset(values)
        if len(choices) != 1:
            raise TaskArtifactError("INVALID_PARAMS")
        name = _artifact_display_name(values["name"])
        description = values.get("description", "Remote M-Claw task output.")
        if (
            not isinstance(description, str)
            or len(description.encode("utf-8")) > 1_024
        ):
            raise TaskArtifactError("INVALID_PARAMS")
        collector = get_task_artifact_collector()
        if collector is None:
            raise TaskArtifactError("ARTIFACT_CONTEXT_UNAVAILABLE")
        if "data" in choices:
            artifact = collector.add_data(
                name=name,
                data=values["data"],
                description=description,
            )
            byte_length = len(protocol.canonical_json_bytes(values["data"]))
        else:
            parent_agent = kwargs.get("parent_agent")
            valid_tools = set(getattr(parent_agent, "valid_tool_names", set()))
            if "read_file" not in valid_tools:
                raise TaskArtifactError("AGENT_TOOLS_FORBIDDEN")
            path = values["path"]
            if not isinstance(path, str) or not path.strip():
                raise TaskArtifactError("INVALID_PARAMS")
            media_type = values.get("media_type", "application/octet-stream")
            if (
                not isinstance(media_type, str)
                or not media_type
                or len(media_type.encode("utf-8")) > 128
            ):
                raise TaskArtifactError("INVALID_PARAMS")
            from mclaw.tools.file_tools import _resolve_agent_path
            from mclaw.runtime.manager import RuntimeManager

            resolved = Path(_resolve_agent_path(path, parent_agent)).expanduser()
            decision = RuntimeManager.current().paths.check("read", resolved)
            if not decision.allowed:
                raise TaskArtifactError("AGENT_TOOLS_FORBIDDEN")
            artifact = collector.add_file(
                name=name,
                path=decision.resolved,
                media_type=media_type,
                description=description,
            )
            transfers = artifact_transfer_parts(artifact)
            if len(transfers) != 1:
                raise TaskArtifactError("ARTIFACT_INVALID")
            byte_length = int(transfers[0][2]["byteLength"])
        return _encode(
            {
                "success": True,
                "artifact_id": artifact["artifactId"],
                "name": name,
                "byte_length": byte_length,
                "_untrustedRemoteData": False,
            }
        )
    except TaskArtifactError as error:
        return _encode(
            {
                "success": False,
                "code": error.code,
                "_untrustedRemoteData": False,
            }
        )
    except DsoftbusRuntimeError as error:
        return _encode(
            {
                "success": False,
                "code": error.code,
                "_untrustedRemoteData": False,
            }
        )
    except Exception:
        return _encode(
            {
                "success": False,
                "code": "INTERNAL_ERROR",
                "_untrustedRemoteData": False,
            }
        )


async def continue_agent_task_handler(
    args: Mapping[str, Any], **kwargs: Any
) -> str:
    device_id = ""
    task_id: str | None = None
    input_request_id: str | None = None
    message_id: str | None = None
    try:
        values = _validate_exact_args(
            args,
            required=frozenset(
                {"device_id", "task_id", "input_request_id"}
            ),
            optional=frozenset({"text", "message_id", "input_paths"}),
        )
        device_id = _device_id(values["device_id"])
        task_id = _canonical_uuid(values["task_id"], optional=False)
        input_request_id = _canonical_uuid(
            values["input_request_id"], optional=False
        )
        message_id = _canonical_uuid(values.get("message_id"), optional=True)
        if message_id is None:
            message_id = str(uuid.uuid4())
        text = values.get("text", "")
        if not isinstance(text, str) or len(text.encode("utf-8")) > 24_576:
            raise DsoftbusRuntimeError("INVALID_PARAMS")
        parent_agent = kwargs.get("parent_agent")
        input_paths = _resolve_task_input_paths(
            values.get("input_paths", []),
            parent_agent,
        )
        if not text and not input_paths:
            raise DsoftbusRuntimeError("INVALID_PARAMS")
        runtime = _runtime_or_failure()
        if runtime is None:
            raise DsoftbusRuntimeError("PEER_NOT_READY")

        def _event_sink(event: Mapping[str, Any]) -> None:
            emit = getattr(parent_agent, "_emit_event", None)
            if callable(emit):
                emit(_plain(event))

        options: dict[str, Any] = {
            "text": text,
            "message_id": message_id,
            "event_sink": _event_sink,
        }
        if input_paths:
            options["input_paths"] = input_paths
        prepare = getattr(
            runtime,
            "prepare_continue_agent_task_outbound",
            None,
        )
        if callable(prepare):
            loop, operation, message_id = prepare(
                device_id,
                task_id,
                input_request_id,
                **options,
            )
            succeeded, value = _run_runtime_outbound(
                operation,
                loop=loop,
                timeout=math.inf,
                label="agent-task-continuation",
                parent_agent=parent_agent,
            )
            if not succeeded:
                raise value
            result = value
        else:
            result = await runtime.acontinue_agent_task(
                device_id,
                task_id,
                input_request_id,
                **options,
            )
        if not isinstance(result, Mapping):
            raise DsoftbusRuntimeError("INTERNAL_ERROR")
        return _encode(dict(result))
    except Exception as error:
        logger.warning(
            "DSoftBus Task continuation failed code=%s task_id=%s "
            "input_request_id=%s",
            getattr(error, "code", "INTERNAL_ERROR"),
            task_id or "",
            input_request_id or "",
            exc_info=True,
        )
        return _handle_error(
            error,
            device_id=device_id,
            task_id=task_id,
            input_request_id=input_request_id,
            message_id=message_id,
        )


async def source_list_handler(args: Mapping[str, Any], **kwargs: Any) -> str:
    try:
        values = _validate_exact_args(
            args,
            required=frozenset({"scope_id"}),
            optional=frozenset({"path", "depth", "page_size", "page_token"}),
        )
        client = get_task_source_client()
        if client is None:
            raise DsoftbusRuntimeError("SOURCE_SCOPE_NOT_FOUND")
        result = await client.list_entries(
            {
                "scopeId": _canonical_uuid(values["scope_id"], optional=False),
                "path": values.get("path", ""),
                "depth": values.get("depth", 1),
                "pageSize": values.get("page_size", 100),
                "pageToken": values.get("page_token", ""),
            }
        )
        return _encode(
            {
                "success": True,
                **dict(result),
                "_untrustedRemoteData": True,
            }
        )
    except Exception as error:
        return _handle_error(error)


async def source_search_handler(args: Mapping[str, Any], **kwargs: Any) -> str:
    try:
        values = _validate_exact_args(
            args,
            required=frozenset({"scope_id", "query"}),
            optional=frozenset({"path", "mode", "max_results"}),
        )
        client = get_task_source_client()
        if client is None:
            raise DsoftbusRuntimeError("SOURCE_SCOPE_NOT_FOUND")
        result = await client.search(
            {
                "scopeId": _canonical_uuid(values["scope_id"], optional=False),
                "path": values.get("path", ""),
                "query": values["query"],
                "mode": values.get("mode", "filename"),
                "maxResults": values.get("max_results", 20),
            }
        )
        return _encode(
            {
                "success": True,
                **dict(result),
                "_untrustedRemoteData": True,
            }
        )
    except Exception as error:
        return _handle_error(error)


async def source_fetch_handler(args: Mapping[str, Any], **kwargs: Any) -> str:
    try:
        values = _validate_exact_args(
            args,
            required=frozenset({"scope_id", "paths"}),
            optional=frozenset(),
        )
        client = get_task_source_client()
        if client is None:
            raise DsoftbusRuntimeError("SOURCE_SCOPE_NOT_FOUND")
        scope_id = _canonical_uuid(values["scope_id"], optional=False)
        paths = values["paths"]
        if not isinstance(paths, (tuple, list)):
            raise DsoftbusRuntimeError("INVALID_PARAMS")
        result = await client.fetch(scope_id=scope_id, paths=paths)
        return _encode(
            {
                "success": True,
                **dict(result),
                "_untrustedRemoteData": True,
            }
        )
    except Exception as error:
        return _handle_error(error)


LIST_PEERS_SCHEMA = {
    "function": {
        "description": "List currently cached OpenHarmony peers without starting network work.",
        "parameters": {
            "type": "object",
            "properties": {"ready_only": {"type": "boolean"}},
            "additionalProperties": False,
        },
    }
}

GET_DEVICE_CONTEXT_SCHEMA = {
    "function": {
        "description": "Read the verified cached Device Context for one peer.",
        "parameters": {
            "type": "object",
            "properties": {
                "device_id": {
                    "type": "string",
                    "pattern": r"^urn:mclaw:device:oh:[0-9a-f]{64}$",
                },
                "refresh_state": {"type": "boolean"},
            },
            "required": ["device_id"],
            "additionalProperties": False,
        },
    }
}

RUN_AGENT_TASK_SCHEMA = {
    "function": {
        "description": "Run one cancelable Agent task on a verified OpenHarmony peer and receive its progress and artifacts.",
        "parameters": {
            "type": "object",
            "properties": {
                "device_id": {
                    "type": "string",
                    "pattern": r"^urn:mclaw:device:oh:[0-9a-f]{64}$",
                },
                "text": {"type": "string", "minLength": 1},
                "context_id": {"type": "string", "format": "uuid"},
                "message_id": {"type": "string", "format": "uuid"},
                "input_paths": {
                    "type": "array",
                    "description": (
                        "Exact local files or directories explicitly in scope for this task. "
                        "Files are sent as verified task copies; directories remain read-only "
                        "sources that the remote Agent browses and fetches only as needed."
                    ),
                    "items": {"type": "string", "minLength": 1},
                    "maxItems": protocol.TASK_INPUT_PATH_MAX,
                },
            },
            "required": ["device_id", "text"],
            "additionalProperties": False,
        },
    }
}

CONTINUE_AGENT_TASK_SCHEMA = {
    "function": {
        "description": (
            "Continue the same remote Agent Task after it requested additional "
            "input. Text, explicit files, and read-only directory scopes are "
            "delivered only to the matching pending request."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "device_id": {
                    "type": "string",
                    "pattern": r"^urn:mclaw:device:oh:[0-9a-f]{64}$",
                },
                "task_id": {"type": "string", "format": "uuid"},
                "input_request_id": {
                    "type": "string",
                    "format": "uuid",
                },
                "text": {"type": "string", "maxLength": 24_576},
                "message_id": {"type": "string", "format": "uuid"},
                "input_paths": {
                    "type": "array",
                    "description": (
                        "Explicit local files or directories supplied for the "
                        "pending request. Files are copied in verified chunks; "
                        "directories become read-only source scopes."
                    ),
                    "items": {"type": "string", "minLength": 1},
                    "maxItems": protocol.TASK_INPUT_PATH_MAX,
                },
            },
            "required": ["device_id", "task_id", "input_request_id"],
            "anyOf": [
                {"required": ["text"]},
                {"required": ["input_paths"]},
            ],
            "additionalProperties": False,
        },
    }
}

SOURCE_LIST_SCHEMA = {
    "function": {
        "description": (
            "Browse a caller-shared read-only directory scope. Results are paginated; "
            "increase depth only when the current task needs nested structure."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "scope_id": {"type": "string", "format": "uuid"},
                "path": {"type": "string", "default": ""},
                "depth": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": protocol.TASK_SOURCE_DEPTH_MAX,
                    "default": 1,
                },
                "page_size": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": protocol.TASK_SOURCE_PAGE_MAX,
                    "default": 100,
                },
                "page_token": {"type": "string", "default": ""},
            },
            "required": ["scope_id"],
            "additionalProperties": False,
        },
    }
}

SOURCE_SEARCH_SCHEMA = {
    "function": {
        "description": (
            "Search file names or bounded UTF-8 text inside a caller-shared read-only "
            "directory scope. Returns paths and short matches, not full files. "
            "When scanLimited is true, narrow the path or query before treating an "
            "empty result as definitive."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "scope_id": {"type": "string", "format": "uuid"},
                "query": {"type": "string", "minLength": 1, "maxLength": 256},
                "path": {"type": "string", "default": ""},
                "mode": {
                    "type": "string",
                    "enum": ["filename", "content"],
                    "default": "filename",
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": protocol.TASK_SOURCE_SEARCH_RESULT_MAX,
                    "default": 20,
                },
            },
            "required": ["scope_id", "query"],
            "additionalProperties": False,
        },
    }
}

SOURCE_FETCH_SCHEMA = {
    "function": {
        "description": (
            "Fetch explicitly selected files from a caller-shared read-only scope into "
            "this Task's local work directory. Returns absolute local working-copy paths."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "scope_id": {"type": "string", "format": "uuid"},
                "paths": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "minItems": 1,
                    "maxItems": protocol.TASK_SOURCE_FETCH_MAX,
                },
            },
            "required": ["scope_id", "paths"],
            "additionalProperties": False,
        },
    }
}

RETURN_ARTIFACT_SCHEMA = {
    "function": {
        "description": (
            "Attach structured JSON or one local file up to 16 MiB to the current "
            "remote Task. A file path is accepted only when read_file is also "
            "available. The framework snapshots and transfers the file in verified "
            "chunks; the peer never receives this device's path."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "data": {},
                "path": {"type": "string", "minLength": 1},
                "description": {"type": "string"},
                "media_type": {"type": "string"},
            },
            "required": ["name"],
            "oneOf": [
                {"required": ["data"], "not": {"required": ["path"]}},
                {"required": ["path"], "not": {"required": ["data"]}},
            ],
            "additionalProperties": False,
        },
    }
}

REQUEST_TASK_INPUT_SCHEMA = {
    "function": {
        "description": (
            "Pause the current remote Task and ask its authenticated caller for "
            "missing text, files, or a read-only directory scope. Call this as the "
            "only tool in the current tool-call batch."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "minLength": 1, "maxLength": 4096},
                "accepts": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["text", "file", "directory"]},
                    "minItems": 1,
                    "maxItems": 3,
                    "uniqueItems": True,
                },
            },
            "required": ["message", "accepts"],
            "additionalProperties": False,
        },
    }
}


def request_task_input_handler(args: Mapping[str, Any], **kwargs: Any) -> str:
    """Register one structured input request for the current inbound Task."""

    try:
        values = _validate_exact_args(
            args,
            required=frozenset({"message", "accepts"}),
            optional=frozenset(),
        )
        collector = get_task_input_request_collector()
        if collector is None:
            raise TaskInputRequestError("TASK_NOT_INPUT_REQUIRED")
        accepts = values["accepts"]
        if not isinstance(accepts, (tuple, list)):
            raise TaskInputRequestError("INVALID_PARAMS")
        request = collector.request(
            message=values["message"],
            accepts=accepts,
        )
        return _encode(
            {
                "success": True,
                "input_required": True,
                **dict(request),
                "_untrustedRemoteData": False,
            }
        )
    except TaskInputRequestError as error:
        return _encode(
            {
                "success": False,
                "code": error.code,
                "_untrustedRemoteData": False,
            }
        )
    except Exception:
        return _encode(
            {
                "success": False,
                "code": "INTERNAL_ERROR",
                "_untrustedRemoteData": False,
            }
        )


registry.register(
    name="dsoftbus_list_peers",
    toolset="dsoftbus",
    schema=LIST_PEERS_SCHEMA,
    handler=list_peers_handler,
    check_fn=dsoftbus_tools_available,
    diagnose_fn=diagnose_dsoftbus_tools,
    emoji="✉",
    max_result_size_chars=protocol.TOOL_RESULT_MAX,
)
registry.register(
    name="dsoftbus_get_device_context",
    toolset="dsoftbus",
    schema=GET_DEVICE_CONTEXT_SCHEMA,
    handler=get_device_context_handler,
    check_fn=dsoftbus_tools_available,
    diagnose_fn=diagnose_dsoftbus_tools,
    is_async=True,
    emoji="✉",
    max_result_size_chars=protocol.TOOL_RESULT_MAX,
)
registry.register(
    name="dsoftbus_run_agent_task",
    toolset="dsoftbus",
    schema=RUN_AGENT_TASK_SCHEMA,
    handler=run_agent_task_handler,
    check_fn=dsoftbus_tools_available,
    diagnose_fn=diagnose_dsoftbus_tools,
    is_async=True,
    emoji="✉",
    async_timeout_seconds=math.inf,
    max_result_size_chars=protocol.TOOL_RESULT_MAX,
)
registry.register(
    name="return_artifact",
    toolset="dsoftbus-artifact",
    schema=RETURN_ARTIFACT_SCHEMA,
    handler=return_artifact_handler,
    max_result_size_chars=4_096,
)
registry.register(
    name="dsoftbus_continue_agent_task",
    toolset="dsoftbus",
    schema=CONTINUE_AGENT_TASK_SCHEMA,
    handler=continue_agent_task_handler,
    check_fn=dsoftbus_tools_available,
    diagnose_fn=diagnose_dsoftbus_tools,
    is_async=True,
    emoji="✉",
    async_timeout_seconds=math.inf,
    max_result_size_chars=protocol.TOOL_RESULT_MAX,
)
registry.register(
    name="request_task_input",
    toolset="dsoftbus-task-control",
    schema=REQUEST_TASK_INPUT_SCHEMA,
    handler=request_task_input_handler,
    emoji="✉",
    max_result_size_chars=4_096,
)
registry.register(
    name="dsoft_bus_source_list",
    toolset="dsoftbus-source",
    schema=SOURCE_LIST_SCHEMA,
    handler=source_list_handler,
    is_async=True,
    emoji="✉",
    async_timeout_seconds=math.inf,
    max_result_size_chars=protocol.TASK_SOURCE_TOOL_RESULT_BYTES_MAX,
)
registry.register(
    name="dsoft_bus_source_search",
    toolset="dsoftbus-source",
    schema=SOURCE_SEARCH_SCHEMA,
    handler=source_search_handler,
    is_async=True,
    emoji="✉",
    async_timeout_seconds=math.inf,
    max_result_size_chars=protocol.TASK_SOURCE_TOOL_RESULT_BYTES_MAX,
)
registry.register(
    name="dsoft_bus_source_fetch",
    toolset="dsoftbus-source",
    schema=SOURCE_FETCH_SCHEMA,
    handler=source_fetch_handler,
    is_async=True,
    emoji="✉",
    async_timeout_seconds=math.inf,
    max_result_size_chars=protocol.TASK_SOURCE_TOOL_RESULT_BYTES_MAX,
)


__all__ = [
    "CONTINUE_AGENT_TASK_SCHEMA",
    "GET_DEVICE_CONTEXT_SCHEMA",
    "LIST_PEERS_SCHEMA",
    "RUN_AGENT_TASK_SCHEMA",
    "SOURCE_FETCH_SCHEMA",
    "SOURCE_LIST_SCHEMA",
    "SOURCE_SEARCH_SCHEMA",
    "RETURN_ARTIFACT_SCHEMA",
    "REQUEST_TASK_INPUT_SCHEMA",
    "diagnose_dsoftbus_tools",
    "dsoftbus_tools_available",
    "continue_agent_task_handler",
    "get_device_context_handler",
    "list_peers_handler",
    "run_agent_task_handler",
    "source_fetch_handler",
    "source_list_handler",
    "source_search_handler",
    "return_artifact_handler",
    "request_task_input_handler",
]
