# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""M-Claw A2A objects carried by authenticated SoftBus byte streams.

This module is deliberately main-process only.  It uses the repository's
existing Pydantic dependency for A2A ProtoJSON objects while keeping the
outer envelope and M-Claw extension objects exact and fail closed.
"""

from __future__ import annotations

import base64
import binascii
import copy
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import math
import re
from types import MappingProxyType
from typing import Any, Literal, Mapping, NoReturn, Sequence
import uuid

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from mclaw import __version__ as MCLAW_VERSION

from . import protocol
from .manifest import MANIFEST_SCHEMA, ManifestError, validate_manifest_descriptor


PRIVATE_PROFILE_LABEL = "mclaw-private-profile/v1"
ERROR_INFO_TYPE = "type.googleapis.com/google.rpc.ErrorInfo"
ERROR_DOMAIN = "mclaw.dsoftbus"

_DEVICE_ID = re.compile(r"^urn:mclaw:device:oh:([0-9a-f]{64})$")
_AGENT_ID = re.compile(r"^urn:mclaw:agent:([0-9a-f]{64})$")
_NONCE = re.compile(r"^[0-9a-f]{64}$")
_SOFTBUS_URL = re.compile(r"^softbus://([0-9a-f]{64})/mclaw\.a2a\.v1$")
_SAFE_METADATA_KEY = re.compile(r"^[A-Za-z0-9._:-]+$")
_URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_RFC3339_UTC = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z$"
)
_MAX_INT32 = 2**31 - 1
_MAX_MESSAGE_TEXT_BYTES = 24_576
_TERMINAL_TASK_STATES = frozenset(
    {
        "TASK_STATE_COMPLETED",
        "TASK_STATE_FAILED",
        "TASK_STATE_CANCELED",
        "TASK_STATE_REJECTED",
    }
)

_METHODS = frozenset(
    {
        "mclaw.binding.open",
        "mclaw.agentCard.get",
        "mclaw.deviceManifest.get",
        "mclaw.deviceState.get",
        "SendMessage",
        "SendStreamingMessage",
        "GetTask",
        "ListTasks",
        "CancelTask",
        "SubscribeToTask",
        "CreateTaskPushNotificationConfig",
        "GetTaskPushNotificationConfig",
        "ListTaskPushNotificationConfigs",
        "DeleteTaskPushNotificationConfig",
        "GetExtendedAgentCard",
    }
)

_HANDSHAKE_PHASES = frozenset(
    {"UNBOUND", "BINDING", "BINDING_OPEN", "CARD_VERIFYING"}
)
_ERROR_MESSAGES: Mapping[str, str] = MappingProxyType(
    {
        "PARSE_ERROR": "Parse error",
        "INVALID_REQUEST": "Invalid request",
        "METHOD_NOT_FOUND": "Method not found",
        "INVALID_PARAMS": "Invalid params",
        "INTERNAL_ERROR": "Internal error",
        "TASK_NOT_FOUND": "Task not found",
        "TASK_NOT_CANCELABLE": "Task is not cancelable",
        "PUSH_NOT_SUPPORTED": "Push notifications are not supported",
        "UNSUPPORTED_OPERATION": "Operation is not supported",
        "CONTENT_TYPE_NOT_SUPPORTED": "Content type is not supported",
        "INVALID_AGENT_RESPONSE": "Invalid agent response",
        "EXTENDED_AGENT_CARD_NOT_CONFIGURED": (
            "Extended agent card is not configured"
        ),
        "EXTENSION_SUPPORT_REQUIRED": "Extension support is required",
        "VERSION_NOT_SUPPORTED": "Version is not supported",
        "PEER_NOT_READY": "Peer is not ready",
        "CAPACITY_BUSY": "Capacity is busy",
        "DEADLINE_EXCEEDED": "Deadline exceeded",
        "FRAME_TOO_LARGE": "Frame is too large",
        "STALE_GENERATION": "Generation is stale",
        "MANIFEST_CONFLICT": "Manifest conflicts with the binding",
        "RUNTIME_STOPPING": "Runtime is stopping",
        "BINDING_INCOMPATIBLE": "Binding is incompatible",
        "AGENT_INTERRUPTED": "Agent execution was interrupted",
        "CONTEXT_NOT_FOUND": "Context not found",
        "REMOTE_INFERENCE_DISABLED": "Remote inference is disabled",
        "RATE_LIMITED": "Rate limit exceeded",
        "REMOTE_BUDGET_EXCEEDED": "Remote budget exceeded",
        "REMOTE_PROVIDER_UNAVAILABLE": "Remote provider is unavailable",
        "PROVIDER_ERROR": "Provider error",
        "AGENT_TOOLS_FORBIDDEN": "Agent tools are forbidden",
    }
)


class A2AError(RuntimeError):
    """Stable JSON-RPC fault without peer-controlled diagnostic text."""

    def __init__(
        self,
        reason: str,
        *,
        metadata: Mapping[str, str] | None = None,
    ) -> None:
        if reason not in protocol.RPC_ERROR_CODES or reason not in _ERROR_MESSAGES:
            raise ValueError("unknown A2A error reason")
        normalized = _validate_error_metadata(metadata or {"outcomeUnknown": "false"})
        super().__init__(reason)
        self.reason = reason
        self.code = protocol.RPC_ERROR_CODES[reason]
        self.message = _ERROR_MESSAGES[reason]
        self.metadata = MappingProxyType(normalized)


def _fail(
    reason: str, *, metadata: Mapping[str, str] | None = None
) -> NoReturn:
    raise A2AError(reason, metadata=metadata)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return copy.deepcopy(value)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return copy.deepcopy(value)


def compact_json_bytes(value: Any) -> bytes:
    """Return sorted compact finite UTF-8 JSON without a record delimiter."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError) as error:
        raise A2AError("INVALID_REQUEST") from error


def _bounded_text(
    value: Any, label: str, minimum: int = 1, maximum: int = 128
) -> str:
    if not isinstance(value, str):
        _fail("INVALID_PARAMS")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise A2AError("INVALID_PARAMS") from error
    if not minimum <= size <= maximum or "\x00" in value:
        _fail("INVALID_PARAMS")
    return value


def _canonical_uuid4(value: Any) -> str:
    try:
        return protocol.canonical_uuid4(value, "id")
    except protocol.ProtocolError as error:
        raise A2AError("INVALID_PARAMS") from error


def _strict_int(value: Any, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail("INVALID_PARAMS")
    return value


def _empty_tenant(value: Any) -> str:
    if value != "":
        _fail("INVALID_PARAMS")
    return ""


def _validate_timestamp(value: Any) -> str:
    text = _bounded_text(value, "timestamp", maximum=40)
    if _RFC3339_UTC.fullmatch(text) is None:
        _fail("INVALID_PARAMS")
    try:
        datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise A2AError("INVALID_PARAMS") from error
    return text


def _measure_json(value: Any, *, max_depth: int, label: str) -> bytes:
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        if depth > max_depth:
            _fail("INVALID_PARAMS")
        if isinstance(current, dict):
            if len(current) > 64:
                _fail("INVALID_PARAMS")
            for key, item in current.items():
                if not isinstance(key, str):
                    _fail("INVALID_PARAMS")
                try:
                    key_bytes = key.encode("utf-8")
                except UnicodeEncodeError as error:
                    raise A2AError("INVALID_PARAMS") from error
                if (
                    not 1 <= len(key_bytes) <= 64
                    or "\x00" in key
                    or _SAFE_METADATA_KEY.fullmatch(key) is None
                ):
                    _fail("INVALID_PARAMS")
                stack.append((item, depth + 1))
        elif isinstance(current, (list, tuple)):
            if len(current) > 64:
                _fail("INVALID_PARAMS")
            stack.extend((item, depth + 1) for item in current)
        elif isinstance(current, str):
            _bounded_text(current, label, minimum=0, maximum=1_024)
        elif current is None or type(current) in {bool, int}:
            continue
        elif type(current) is float:
            if not math.isfinite(current):
                _fail("INVALID_PARAMS")
        else:
            _fail("INVALID_PARAMS")
    return compact_json_bytes(_plain(value))


def _validate_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail("INVALID_PARAMS")
    encoded = _measure_json(value, max_depth=8, label="metadata")
    if len(encoded) > 4_096:
        _fail("INVALID_PARAMS")
    return copy.deepcopy(value)


def _validate_string_list(
    value: Any,
    *,
    maximum_items: int,
    maximum_bytes: int,
    uri: bool = False,
) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum_items:
        _fail("INVALID_PARAMS")
    result: list[str] = []
    for item in value:
        text = _bounded_text(item, "string list item", maximum=maximum_bytes)
        if uri and (
            _URI_SCHEME.match(text) is None
            or any(character.isspace() for character in text)
        ):
            _fail("INVALID_PARAMS")
        result.append(text)
    if len(result) != len(set(result)):
        _fail("INVALID_PARAMS")
    return result


def _validate_error_metadata(value: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(value, Mapping) or not 1 <= len(value) <= 8:
        raise ValueError("error metadata must contain 1..8 entries")
    allowed = frozenset({"outcomeUnknown", "failureReason"})
    result: dict[str, str] = {}
    for key, item in value.items():
        if key not in allowed or not isinstance(item, str):
            raise ValueError("error metadata contains an unsafe field")
        if key == "outcomeUnknown" and item not in {"true", "false"}:
            raise ValueError("outcomeUnknown must be a string boolean")
        if key == "failureReason" and item not in protocol.RPC_ERROR_CODES:
            raise ValueError("failureReason must be stable")
        result[key] = item
    if "outcomeUnknown" not in result:
        result["outcomeUnknown"] = "false"
    return result


@dataclass(frozen=True, slots=True)
class ServiceParameters:
    values: Mapping[str, str]
    extensions: frozenset[str]

    def advertises(self, uri: str) -> bool:
        return uri in self.extensions


def validate_service_parameters(value: Any) -> ServiceParameters:
    if not isinstance(value, dict) or len(value) > protocol.SERVICE_PARAMETER_MAX:
        _fail("INVALID_REQUEST")
    normalized: dict[str, tuple[str, str]] = {}
    total = 0
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            _fail("INVALID_REQUEST")
        try:
            key_bytes = key.encode("ascii")
            value_bytes = item.encode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError) as error:
            raise A2AError("INVALID_REQUEST") from error
        if (
            not 1 <= len(key_bytes) <= protocol.SERVICE_PARAMETER_KEY_MAX
            or len(value_bytes) > protocol.SERVICE_PARAMETER_VALUE_MAX
            or "\x00" in item
        ):
            _fail("INVALID_REQUEST")
        total += len(key_bytes) + len(value_bytes)
        lowered = key.lower()
        if lowered in normalized:
            _fail("INVALID_REQUEST")
        normalized[lowered] = (key, item)
    if total > protocol.SERVICE_PARAMETERS_BYTES_MAX:
        _fail("INVALID_REQUEST")
    allowed = frozenset({"a2a-version", "a2a-extensions"})
    if set(normalized) - allowed or "a2a-version" not in normalized:
        _fail("INVALID_REQUEST")
    if normalized["a2a-version"][1] != protocol.A2A_PROTOCOL_VERSION:
        _fail("VERSION_NOT_SUPPORTED")
    extensions: list[str] = []
    if "a2a-extensions" in normalized:
        raw_extensions = normalized["a2a-extensions"][1].split(",")
        for raw in raw_extensions:
            extension = raw.strip()
            if (
                not extension
                or len(extension.encode("utf-8")) > 2_048
                or _URI_SCHEME.match(extension) is None
                or any(character.isspace() for character in extension)
            ):
                _fail("INVALID_REQUEST")
            extensions.append(extension)
        if len(extensions) != len(set(extensions)):
            _fail("INVALID_REQUEST")
    canonical = {"A2A-Version": protocol.A2A_PROTOCOL_VERSION}
    if extensions:
        canonical["A2A-Extensions"] = ",".join(extensions)
    return ServiceParameters(MappingProxyType(canonical), frozenset(extensions))


def _json_depth_valid(value: Any, maximum: int = 16) -> bool:
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        if depth > maximum:
            return False
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
    return True


def _effective_frame_cap(negotiated_mtu: int) -> int:
    if (
        type(negotiated_mtu) is not int
        or negotiated_mtu < protocol.MIN_NEGOTIATED_FRAME
    ):
        _fail("BINDING_INCOMPATIBLE")
    return min(protocol.REMOTE_FRAME_MAX, negotiated_mtu)


@dataclass(frozen=True, slots=True)
class RequestEnvelope:
    request_id: str
    method: str
    params: Mapping[str, Any]
    service_parameters: ServiceParameters
    byte_length: int


@dataclass(frozen=True, slots=True)
class ResponseEnvelope:
    request_id: str | None
    result: Any | None
    error: Mapping[str, Any] | None
    byte_length: int


def _load_frame(raw: bytes, negotiated_mtu: int) -> dict[str, Any]:
    cap = _effective_frame_cap(negotiated_mtu)
    if not isinstance(raw, bytes) or len(raw) > cap:
        _fail("FRAME_TOO_LARGE")
    if raw[:1] in {b" ", b"\t", b"\r", b"\n"}:
        _fail("PARSE_ERROR")
    try:
        value = protocol.strict_json_loads(raw, max_bytes=cap, require_object=True)
    except protocol.ProtocolError as error:
        reason = "PARSE_ERROR" if error.code == "PARSE_ERROR" else "INVALID_REQUEST"
        raise A2AError(reason) from error
    if not _json_depth_valid(value):
        _fail("INVALID_REQUEST")
    return value


def _exact(value: Any, keys: frozenset[str], reason: str = "INVALID_REQUEST") -> dict[str, Any]:
    if not isinstance(value, dict) or frozenset(value) != keys:
        _fail(reason)
    return value


def _recognized(
    value: Any,
    keys: frozenset[str],
    *,
    require_exact: bool,
) -> dict[str, Any]:
    """Select standard A2A fields while permitting future unknown fields."""

    if not isinstance(value, dict) or not keys.issubset(value):
        _fail("INVALID_REQUEST")
    if require_exact and frozenset(value) != keys:
        _fail("INVALID_REQUEST")
    return {key: value[key] for key in keys}


def parse_request_frame(raw: bytes, *, negotiated_mtu: int) -> RequestEnvelope:
    root = _exact(
        _load_frame(raw, negotiated_mtu),
        frozenset({"binding", "bindingVersion", "serviceParameters", "rpc"}),
    )
    if (
        root["binding"] != protocol.PROTOCOL_BINDING
        or type(root["bindingVersion"]) is not int
        or root["bindingVersion"] != protocol.BINDING_VERSION
    ):
        _fail("BINDING_INCOMPATIBLE")
    service_parameters = validate_service_parameters(root["serviceParameters"])
    rpc = _exact(
        root["rpc"], frozenset({"jsonrpc", "id", "method", "params"})
    )
    if rpc["jsonrpc"] != "2.0":
        _fail("INVALID_REQUEST")
    try:
        request_id = protocol.canonical_uuid4(rpc["id"], "rpc.id")
    except protocol.ProtocolError as error:
        raise A2AError("INVALID_REQUEST") from error
    method = rpc["method"]
    try:
        method_size = len(method.encode("utf-8")) if isinstance(method, str) else 0
    except UnicodeEncodeError as error:
        raise A2AError("INVALID_REQUEST") from error
    if not 1 <= method_size <= 128:
        _fail("INVALID_REQUEST")
    if not isinstance(rpc["params"], dict):
        _fail("INVALID_REQUEST")
    return RequestEnvelope(
        request_id=request_id,
        method=method,
        params=_freeze(rpc["params"]),
        service_parameters=service_parameters,
        byte_length=len(raw),
    )


def _validate_error_object(value: Any) -> Mapping[str, Any]:
    error = _exact(value, frozenset({"code", "message", "data"}))
    reason = next(
        (
            candidate
            for candidate, code in protocol.RPC_ERROR_CODES.items()
            if code == error["code"]
        ),
        None,
    )
    if reason is None or error["message"] != _ERROR_MESSAGES[reason]:
        _fail("INVALID_REQUEST")
    if not isinstance(error["data"], list) or len(error["data"]) != 1:
        _fail("INVALID_REQUEST")
    info = _exact(
        error["data"][0],
        frozenset({"@type", "reason", "domain", "metadata"}),
    )
    if (
        info["@type"] != ERROR_INFO_TYPE
        or info["reason"] != reason
        or info["domain"] != ERROR_DOMAIN
    ):
        _fail("INVALID_REQUEST")
    try:
        metadata = _validate_error_metadata(info["metadata"])
    except ValueError as validation_error:
        raise A2AError("INVALID_REQUEST") from validation_error
    normalized = copy.deepcopy(error)
    normalized["data"][0]["metadata"] = metadata
    return _freeze(normalized)


def parse_response_frame(raw: bytes, *, negotiated_mtu: int) -> ResponseEnvelope:
    root = _exact(
        _load_frame(raw, negotiated_mtu),
        frozenset({"binding", "bindingVersion", "rpc"}),
    )
    if (
        root["binding"] != protocol.PROTOCOL_BINDING
        or type(root["bindingVersion"]) is not int
        or root["bindingVersion"] != protocol.BINDING_VERSION
    ):
        _fail("BINDING_INCOMPATIBLE")
    rpc = root["rpc"]
    if not isinstance(rpc, dict) or rpc.get("jsonrpc") != "2.0":
        _fail("INVALID_REQUEST")
    has_result = frozenset(rpc) == frozenset({"jsonrpc", "id", "result"})
    has_error = frozenset(rpc) == frozenset({"jsonrpc", "id", "error"})
    if has_result == has_error:
        _fail("INVALID_REQUEST")
    request_id = rpc["id"]
    if request_id is None:
        if not has_error:
            _fail("INVALID_REQUEST")
    else:
        try:
            request_id = protocol.canonical_uuid4(request_id, "rpc.id")
        except protocol.ProtocolError as error:
            raise A2AError("INVALID_REQUEST") from error
    if has_error:
        return ResponseEnvelope(
            request_id=request_id,
            result=None,
            error=_validate_error_object(rpc["error"]),
            byte_length=len(raw),
        )
    return ResponseEnvelope(
        request_id=request_id,
        result=_freeze(rpc["result"]),
        error=None,
        byte_length=len(raw),
    )


def build_rpc_request(
    method: str,
    params: Mapping[str, Any],
    *,
    request_id: str | None = None,
) -> Mapping[str, Any]:
    try:
        method_size = len(method.encode("utf-8")) if isinstance(method, str) else 0
    except UnicodeEncodeError as error:
        raise A2AError("INVALID_REQUEST") from error
    if not 1 <= method_size <= 128:
        _fail("INVALID_REQUEST")
    if not isinstance(params, Mapping):
        _fail("INVALID_REQUEST")
    if request_id is None:
        request_id = str(uuid.uuid4())
    try:
        request_id = protocol.canonical_uuid4(request_id, "rpc.id")
    except protocol.ProtocolError as error:
        raise A2AError("INVALID_REQUEST") from error
    return MappingProxyType(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": _freeze(dict(params)),
        }
    )


def build_rpc_success(request_id: str, result: Any) -> Mapping[str, Any]:
    try:
        normalized_id = protocol.canonical_uuid4(request_id, "rpc.id")
    except protocol.ProtocolError as error:
        raise A2AError("INVALID_REQUEST") from error
    return MappingProxyType(
        {"jsonrpc": "2.0", "id": normalized_id, "result": _freeze(result)}
    )


def build_rpc_error(
    request_id: str | None,
    reason: str,
    *,
    outcome_unknown: bool = False,
    failure_reason: str | None = None,
) -> Mapping[str, Any]:
    if request_id is not None:
        try:
            request_id = protocol.canonical_uuid4(request_id, "rpc.id")
        except protocol.ProtocolError as error:
            raise A2AError("INVALID_REQUEST") from error
    if type(outcome_unknown) is not bool:
        raise TypeError("outcome_unknown must be bool")
    metadata = {"outcomeUnknown": "true" if outcome_unknown else "false"}
    if failure_reason is not None:
        metadata["failureReason"] = failure_reason
    fault = A2AError(reason, metadata=metadata)
    result = {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": fault.code,
            "message": fault.message,
            "data": [
                {
                    "@type": ERROR_INFO_TYPE,
                    "reason": reason,
                    "domain": ERROR_DOMAIN,
                    "metadata": dict(fault.metadata),
                }
            ],
        },
    }
    _validate_error_object(result["error"])
    return _freeze(result)


@dataclass(frozen=True, slots=True)
class EncodedFrame:
    data: bytes | None
    close_generation: bool
    terminal_reason: str | None


def _outer_for_rpc(
    rpc: Mapping[str, Any], service_parameters: Mapping[str, str] | None
) -> dict[str, Any]:
    plain_rpc = _plain(rpc)
    if "method" in plain_rpc:
        request = _exact(
            plain_rpc, frozenset({"jsonrpc", "id", "method", "params"})
        )
        if request["jsonrpc"] != "2.0" or not isinstance(request["params"], dict):
            _fail("INVALID_REQUEST")
        try:
            protocol.canonical_uuid4(request["id"], "rpc.id")
        except protocol.ProtocolError as error:
            raise A2AError("INVALID_REQUEST") from error
        try:
            method_size = (
                len(request["method"].encode("utf-8"))
                if isinstance(request["method"], str)
                else 0
            )
        except UnicodeEncodeError as error:
            raise A2AError("INVALID_REQUEST") from error
        if not 1 <= method_size <= 128:
            _fail("INVALID_REQUEST")
        if service_parameters is None:
            service_parameters = {"A2A-Version": protocol.A2A_PROTOCOL_VERSION}
        validated = validate_service_parameters(dict(service_parameters))
        return {
            "binding": protocol.PROTOCOL_BINDING,
            "bindingVersion": protocol.BINDING_VERSION,
            "serviceParameters": dict(validated.values),
            "rpc": plain_rpc,
        }
    if service_parameters is not None:
        _fail("INVALID_REQUEST")
    if not isinstance(plain_rpc, dict) or plain_rpc.get("jsonrpc") != "2.0":
        _fail("INVALID_REQUEST")
    success = frozenset(plain_rpc) == frozenset({"jsonrpc", "id", "result"})
    failure = frozenset(plain_rpc) == frozenset({"jsonrpc", "id", "error"})
    if success == failure:
        _fail("INVALID_REQUEST")
    if plain_rpc["id"] is None:
        if not failure:
            _fail("INVALID_REQUEST")
    else:
        try:
            protocol.canonical_uuid4(plain_rpc["id"], "rpc.id")
        except protocol.ProtocolError as error:
            raise A2AError("INVALID_REQUEST") from error
    if failure:
        _validate_error_object(plain_rpc["error"])
    return {
        "binding": protocol.PROTOCOL_BINDING,
        "bindingVersion": protocol.BINDING_VERSION,
        "rpc": plain_rpc,
    }


def encode_frame_or_error(
    rpc: Mapping[str, Any],
    *,
    phase: str,
    negotiated_mtu: int,
    service_parameters: Mapping[str, str] | None = None,
    inbound_request_id: str | None = None,
) -> EncodedFrame:
    """The sole outer-frame encoder, including bounded oversize fallback."""

    if phase not in _HANDSHAKE_PHASES | {"READY"}:
        raise ValueError("phase is invalid")
    cap = _effective_frame_cap(negotiated_mtu)
    outer = _outer_for_rpc(rpc, service_parameters)
    if not _json_depth_valid(outer):
        _fail("INVALID_REQUEST")
    encoded = compact_json_bytes(outer)
    if len(encoded) <= cap:
        if "method" in outer["rpc"]:
            parse_request_frame(encoded, negotiated_mtu=negotiated_mtu)
        else:
            parse_response_frame(encoded, negotiated_mtu=negotiated_mtu)
        return EncodedFrame(encoded, False, None)
    if inbound_request_id is None:
        _fail("FRAME_TOO_LARGE")
    handshake = phase in _HANDSHAKE_PHASES
    reason = "BINDING_INCOMPATIBLE" if handshake else "FRAME_TOO_LARGE"
    fallback_rpc = build_rpc_error(
        inbound_request_id,
        reason,
        failure_reason="FRAME_TOO_LARGE" if handshake else None,
    )
    fallback = compact_json_bytes(_outer_for_rpc(fallback_rpc, None))
    if len(fallback) > min(cap, protocol.TERMINAL_ERROR_FRAME_MAX):
        return EncodedFrame(None, True, reason)
    parse_response_frame(fallback, negotiated_mtu=negotiated_mtu)
    return EncodedFrame(fallback, handshake, reason)


def encode_request_frame(
    method: str,
    params: Mapping[str, Any],
    *,
    negotiated_mtu: int,
    request_id: str | None = None,
    extensions: tuple[str, ...] = (),
    phase: str = "READY",
) -> EncodedFrame:
    service_parameters = {"A2A-Version": protocol.A2A_PROTOCOL_VERSION}
    if extensions:
        service_parameters["A2A-Extensions"] = ",".join(extensions)
    return encode_frame_or_error(
        build_rpc_request(method, params, request_id=request_id),
        phase=phase,
        negotiated_mtu=negotiated_mtu,
        service_parameters=service_parameters,
    )


def encode_success_frame(
    request_id: str,
    result: Any,
    *,
    negotiated_mtu: int,
    phase: str,
) -> EncodedFrame:
    return encode_frame_or_error(
        build_rpc_success(request_id, result),
        phase=phase,
        negotiated_mtu=negotiated_mtu,
        inbound_request_id=request_id,
    )


def encode_error_frame(
    request_id: str | None,
    reason: str,
    *,
    negotiated_mtu: int,
    phase: str,
    outcome_unknown: bool = False,
) -> EncodedFrame:
    rpc = build_rpc_error(
        request_id, reason, outcome_unknown=outcome_unknown
    )
    encoded = compact_json_bytes(_outer_for_rpc(rpc, None))
    cap = _effective_frame_cap(negotiated_mtu)
    if len(encoded) > min(cap, protocol.TERMINAL_ERROR_FRAME_MAX):
        return EncodedFrame(None, True, reason)
    parse_response_frame(encoded, negotiated_mtu=negotiated_mtu)
    return EncodedFrame(
        encoded,
        request_id is None or phase in _HANDSHAKE_PHASES,
        reason,
    )


class _A2AModel(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)


class _Part(_A2AModel):
    text: str | None = None
    raw: str | None = None
    url: str | None = None
    data: Any = None
    metadata: dict[str, Any] | None = None
    filename: str | None = None
    mediaType: str | None = None

    @model_validator(mode="after")
    def validate_part(self) -> "_Part":
        choices = {"text", "raw", "url", "data"} & self.model_fields_set
        if len(choices) != 1:
            raise ValueError("Part content oneof")
        if self.metadata is not None:
            _validate_metadata(self.metadata)
        if "text" in choices:
            assert self.text is not None
            _bounded_text(
                self.text,
                "text",
                minimum=0,
                maximum=_MAX_MESSAGE_TEXT_BYTES,
            )
        if "raw" in choices:
            assert self.raw is not None
            raw = _bounded_text(
                self.raw,
                "raw",
                maximum=_MAX_MESSAGE_TEXT_BYTES,
            )
            try:
                encoded = raw.encode("ascii")
                decoded = base64.b64decode(encoded, validate=True)
            except (UnicodeEncodeError, binascii.Error, ValueError) as error:
                raise ValueError("raw must be canonical Base64") from error
            if base64.b64encode(decoded) != encoded:
                raise ValueError("raw must be canonical Base64")
        if "url" in choices:
            assert self.url is not None
            _bounded_text(self.url, "url", maximum=2_048)
        if "data" in choices:
            encoded = _measure_json(
                self.data,
                max_depth=16,
                label="part.data",
            )
            if len(encoded) > _MAX_MESSAGE_TEXT_BYTES:
                _fail("INVALID_PARAMS")
        if self.filename is not None:
            _bounded_text(self.filename, "filename", maximum=1_024)
        if self.mediaType is not None:
            _bounded_text(self.mediaType, "mediaType", minimum=0, maximum=128)
        return self


class _Message(_A2AModel):
    messageId: str
    contextId: str | None = None
    taskId: str | None = None
    role: Literal["ROLE_USER", "ROLE_AGENT"]
    parts: list[_Part]
    metadata: dict[str, Any] | None = None
    extensions: list[str] | None = None
    referenceTaskIds: list[str] | None = None

    @model_validator(mode="after")
    def validate_message(self) -> "_Message":
        _canonical_uuid4(self.messageId)
        if self.contextId is not None:
            _canonical_uuid4(self.contextId)
        if self.taskId not in {None, ""}:
            _bounded_text(self.taskId, "taskId")
        if not 1 <= len(self.parts) <= 32:
            raise ValueError("parts count")
        if self.metadata is not None:
            _validate_metadata(self.metadata)
        if self.extensions is not None:
            _validate_string_list(
                self.extensions, maximum_items=32, maximum_bytes=2_048, uri=True
            )
        if self.referenceTaskIds is not None:
            _validate_string_list(
                self.referenceTaskIds, maximum_items=32, maximum_bytes=128
            )
        return self


_TASK_STATES = Literal[
    "TASK_STATE_UNSPECIFIED",
    "TASK_STATE_SUBMITTED",
    "TASK_STATE_WORKING",
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_INPUT_REQUIRED",
    "TASK_STATE_REJECTED",
    "TASK_STATE_AUTH_REQUIRED",
]


class _TaskStatus(_A2AModel):
    state: _TASK_STATES
    message: _Message | None = None
    timestamp: str | None = None

    @model_validator(mode="after")
    def validate_status(self) -> "_TaskStatus":
        if self.timestamp is not None:
            _validate_timestamp(self.timestamp)
        return self


class _Artifact(_A2AModel):
    artifactId: str
    name: str | None = None
    description: str | None = None
    parts: list[_Part]
    metadata: dict[str, Any] | None = None
    extensions: list[str] | None = None

    @model_validator(mode="after")
    def validate_artifact(self) -> "_Artifact":
        _canonical_uuid4(self.artifactId)
        if self.name is not None:
            _bounded_text(self.name, "artifact.name", maximum=1_024)
        if self.description is not None:
            _bounded_text(
                self.description,
                "artifact.description",
                maximum=1_024,
            )
        if not 1 <= len(self.parts) <= 32:
            raise ValueError("artifact parts count")
        if self.metadata is not None:
            _validate_metadata(self.metadata)
        if self.extensions is not None:
            _validate_string_list(
                self.extensions,
                maximum_items=32,
                maximum_bytes=2_048,
                uri=True,
            )
        return self


class _Task(_A2AModel):
    id: str
    contextId: str
    status: _TaskStatus
    artifacts: list[_Artifact] | None = None
    history: list[_Message] | None = None
    metadata: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_task(self) -> "_Task":
        task_id = _canonical_uuid4(self.id)
        context_id = _canonical_uuid4(self.contextId)
        if self.artifacts is not None:
            if len(self.artifacts) > protocol.TASK_ARTIFACT_MAX:
                raise ValueError("task artifact count")
            artifact_ids = [item.artifactId for item in self.artifacts]
            if len(artifact_ids) != len(set(artifact_ids)):
                raise ValueError("duplicate artifact id")
        if self.history is not None:
            if len(self.history) > protocol.TASK_HISTORY_MAX:
                raise ValueError("task history count")
            for message in self.history:
                if message.contextId not in {None, context_id}:
                    raise ValueError("task history context mismatch")
                if message.taskId not in {None, "", task_id}:
                    raise ValueError("task history id mismatch")
        message = self.status.message
        if message is not None:
            if (
                message.role != "ROLE_AGENT"
                or message.contextId != context_id
                or message.taskId != task_id
            ):
                raise ValueError("task status message mismatch")
        if self.metadata is not None:
            _validate_metadata(self.metadata)
        return self


class _TaskStatusUpdateEvent(_A2AModel):
    taskId: str
    contextId: str
    status: _TaskStatus
    metadata: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_update(self) -> "_TaskStatusUpdateEvent":
        task_id = _canonical_uuid4(self.taskId)
        context_id = _canonical_uuid4(self.contextId)
        message = self.status.message
        if message is not None and (
            message.role != "ROLE_AGENT"
            or message.taskId != task_id
            or message.contextId != context_id
        ):
            raise ValueError("status update message mismatch")
        if self.metadata is not None:
            _validate_metadata(self.metadata)
        return self


class _TaskArtifactUpdateEvent(_A2AModel):
    taskId: str
    contextId: str
    artifact: _Artifact
    append: bool | None = None
    lastChunk: bool | None = None
    metadata: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_update(self) -> "_TaskArtifactUpdateEvent":
        _canonical_uuid4(self.taskId)
        _canonical_uuid4(self.contextId)
        if self.metadata is not None:
            _validate_metadata(self.metadata)
        return self


class _SendMessageResponse(_A2AModel):
    task: _Task | None = None
    message: _Message | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> "_SendMessageResponse":
        if len({"task", "message"} & self.model_fields_set) != 1:
            raise ValueError("send response payload oneof")
        return self


class _StreamResponse(_A2AModel):
    task: _Task | None = None
    message: _Message | None = None
    statusUpdate: _TaskStatusUpdateEvent | None = None
    artifactUpdate: _TaskArtifactUpdateEvent | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> "_StreamResponse":
        if len(
            {"task", "message", "statusUpdate", "artifactUpdate"}
            & self.model_fields_set
        ) != 1:
            raise ValueError("stream response payload oneof")
        return self


class _AuthenticationInfo(_A2AModel):
    scheme: str
    credentials: str | None = None

    @model_validator(mode="after")
    def validate_authentication(self) -> "_AuthenticationInfo":
        _bounded_text(self.scheme, "scheme", maximum=128)
        if self.credentials is not None:
            _bounded_text(self.credentials, "credentials", maximum=1_024)
        return self


class _TaskPushNotificationConfig(_A2AModel):
    tenant: str | None = None
    id: str | None = None
    taskId: str | None = None
    url: str
    token: str | None = None
    authentication: _AuthenticationInfo | None = None

    @model_validator(mode="after")
    def validate_config(self) -> "_TaskPushNotificationConfig":
        if self.tenant is not None:
            _empty_tenant(self.tenant)
        for item in (self.id, self.taskId):
            if item is not None:
                _bounded_text(item, "id")
        _bounded_text(self.url, "url", maximum=2_048)
        if self.token is not None:
            _bounded_text(self.token, "token", maximum=1_024)
        return self


class _SendConfiguration(_A2AModel):
    acceptedOutputModes: list[str] | None = None
    taskPushNotificationConfig: _TaskPushNotificationConfig | None = None
    historyLength: int | None = Field(default=None, ge=0, le=_MAX_INT32)
    returnImmediately: bool | None = None

    @field_validator("historyLength", mode="before")
    @classmethod
    def strict_history(cls, value: Any) -> Any:
        if value is not None and type(value) is not int:
            raise ValueError("historyLength must be strict integer")
        return value

    @model_validator(mode="after")
    def validate_configuration(self) -> "_SendConfiguration":
        if self.acceptedOutputModes is not None:
            values = _validate_string_list(
                self.acceptedOutputModes, maximum_items=32, maximum_bytes=128
            )
            if values and values != ["text/plain"]:
                raise ValueError("acceptedOutputModes")
        return self


class _SendMessageRequest(_A2AModel):
    tenant: str | None = None
    message: _Message
    configuration: _SendConfiguration | None = None
    metadata: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_request(self) -> "_SendMessageRequest":
        if self.tenant is not None:
            _empty_tenant(self.tenant)
        if self.metadata is not None:
            _validate_metadata(self.metadata)
        return self


class _GetTaskRequest(_A2AModel):
    tenant: str | None = None
    id: str
    historyLength: int | None = Field(default=None, ge=0, le=_MAX_INT32)

    @model_validator(mode="after")
    def validate_request(self) -> "_GetTaskRequest":
        if self.tenant is not None:
            _empty_tenant(self.tenant)
        _bounded_text(self.id, "id")
        if self.historyLength is not None:
            _strict_int(self.historyLength, 0, _MAX_INT32)
        return self


class _ListTasksRequest(_A2AModel):
    tenant: str | None = None
    contextId: str | None = None
    status: _TASK_STATES | None = None
    pageSize: int | None = Field(default=None, ge=1, le=100)
    pageToken: str | None = None
    historyLength: int | None = Field(default=None, ge=0, le=_MAX_INT32)
    statusTimestampAfter: str | None = None
    includeArtifacts: bool | None = None

    @model_validator(mode="after")
    def validate_request(self) -> "_ListTasksRequest":
        if self.tenant is not None:
            _empty_tenant(self.tenant)
        if self.contextId is not None:
            _canonical_uuid4(self.contextId)
        if self.pageSize is not None:
            _strict_int(self.pageSize, 1, 100)
        if self.pageToken not in {None, ""}:
            raise ValueError("pageToken")
        if self.historyLength is not None:
            _strict_int(self.historyLength, 0, _MAX_INT32)
        if self.statusTimestampAfter is not None:
            _validate_timestamp(self.statusTimestampAfter)
        return self


class _CancelTaskRequest(_A2AModel):
    tenant: str | None = None
    id: str
    metadata: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_request(self) -> "_CancelTaskRequest":
        if self.tenant is not None:
            _empty_tenant(self.tenant)
        _bounded_text(self.id, "id")
        if self.metadata is not None:
            _validate_metadata(self.metadata)
        return self


class _SubscribeRequest(_A2AModel):
    tenant: str | None = None
    id: str

    @model_validator(mode="after")
    def validate_request(self) -> "_SubscribeRequest":
        if self.tenant is not None:
            _empty_tenant(self.tenant)
        _bounded_text(self.id, "id")
        return self


class _ExtendedCardRequest(_A2AModel):
    tenant: str | None = None

    @model_validator(mode="after")
    def validate_request(self) -> "_ExtendedCardRequest":
        if self.tenant is not None:
            _empty_tenant(self.tenant)
        return self


class _PushItemRequest(_A2AModel):
    tenant: str | None = None
    taskId: str
    id: str

    @model_validator(mode="after")
    def validate_request(self) -> "_PushItemRequest":
        if self.tenant is not None:
            _empty_tenant(self.tenant)
        _bounded_text(self.taskId, "taskId")
        _bounded_text(self.id, "id")
        return self


class _PushListRequest(_A2AModel):
    tenant: str | None = None
    taskId: str
    pageSize: int | None = Field(default=None, ge=1, le=100)
    pageToken: str | None = None

    @model_validator(mode="after")
    def validate_request(self) -> "_PushListRequest":
        if self.tenant is not None:
            _empty_tenant(self.tenant)
        _bounded_text(self.taskId, "taskId")
        if self.pageSize is not None:
            _strict_int(self.pageSize, 1, 100)
        if self.pageToken is not None:
            _bounded_text(self.pageToken, "pageToken", minimum=0, maximum=1_024)
        return self


def _model(model: type[BaseModel], params: Any) -> BaseModel:
    try:
        return model.model_validate(params)
    except A2AError:
        raise
    except (ValidationError, ValueError, TypeError) as error:
        raise A2AError("INVALID_PARAMS") from error


def _validate_binding_open(params: Any) -> dict[str, Any]:
    value = _exact(
        params,
        frozenset(
            {
                "deviceId",
                "agentId",
                "runtimeInstanceId",
                "initiatorDeviceId",
                "connectionNonce",
                "bindingVersion",
                "a2aVersion",
                "manifest",
            }
        ),
        "INVALID_PARAMS",
    )
    if not isinstance(value["deviceId"], str) or _DEVICE_ID.fullmatch(
        value["deviceId"]
    ) is None:
        _fail("INVALID_PARAMS")
    if not isinstance(value["agentId"], str) or _AGENT_ID.fullmatch(
        value["agentId"]
    ) is None:
        _fail("INVALID_PARAMS")
    if not isinstance(value["initiatorDeviceId"], str) or _DEVICE_ID.fullmatch(
        value["initiatorDeviceId"]
    ) is None:
        _fail("INVALID_PARAMS")
    _canonical_uuid4(value["runtimeInstanceId"])
    if not isinstance(value["connectionNonce"], str) or _NONCE.fullmatch(
        value["connectionNonce"]
    ) is None:
        _fail("INVALID_PARAMS")
    if (
        type(value["bindingVersion"]) is not int
        or value["bindingVersion"] != protocol.BINDING_VERSION
        or value["a2aVersion"] != protocol.A2A_PROTOCOL_VERSION
    ):
        _fail("BINDING_INCOMPATIBLE")
    try:
        validate_manifest_descriptor(value["manifest"])
    except ManifestError as error:
        raise A2AError("INVALID_PARAMS") from error
    return copy.deepcopy(value)


@dataclass(frozen=True, slots=True)
class CoreMethodCall:
    method: str
    params: Mapping[str, Any]
    normalized_text: str | None = None


def validate_core_method(method: str, params: Any) -> CoreMethodCall | Mapping[str, Any]:
    """Validate a recognized method and return a dispatch or static result.

    Methods whose current capability is disabled raise their stable terminal
    fault only after their request object has been validated.
    """

    if method not in _METHODS:
        _fail("METHOD_NOT_FOUND")
    if not isinstance(params, Mapping):
        _fail("INVALID_PARAMS")
    params = _plain(params)
    if method == "mclaw.binding.open":
        normalized = _validate_binding_open(params)
        return CoreMethodCall(method, _freeze(normalized))
    if method == "mclaw.agentCard.get":
        _exact(params, frozenset(), "INVALID_PARAMS")
        return CoreMethodCall(method, MappingProxyType({}))
    if method == "mclaw.deviceManifest.get":
        if params == {}:
            return CoreMethodCall(method, MappingProxyType({}))
        value = _exact(
            params, frozenset({"ifRevision", "ifDigest"}), "INVALID_PARAMS"
        )
        _strict_int(value["ifRevision"], 1, 2**63 - 1)
        try:
            validate_manifest_descriptor(
                {
                    "schemaVersion": MANIFEST_SCHEMA,
                    "revision": value["ifRevision"],
                    "digest": value["ifDigest"],
                }
            )
        except ManifestError as error:
            raise A2AError("INVALID_PARAMS") from error
        return CoreMethodCall(method, _freeze(copy.deepcopy(value)))
    if method == "mclaw.deviceState.get":
        if params == {}:
            return CoreMethodCall(method, MappingProxyType({}))
        value = _exact(params, frozenset({"resourceIds"}), "INVALID_PARAMS")
        ids = _validate_string_list(
            value["resourceIds"], maximum_items=128, maximum_bytes=128
        )
        resource_pattern = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
        if any(resource_pattern.fullmatch(item) is None for item in ids):
            _fail("INVALID_PARAMS")
        return CoreMethodCall(method, _freeze({"resourceIds": ids}))
    if method in {"SendMessage", "SendStreamingMessage"}:
        request = _model(_SendMessageRequest, params)
        assert isinstance(request, _SendMessageRequest)
        message = request.message
        if message.role != "ROLE_USER":
            _fail("INVALID_PARAMS")
        if message.taskId not in {None, ""} or message.referenceTaskIds:
            _fail("TASK_NOT_FOUND")
        text_parts: list[str] = []
        for part in message.parts:
            if (
                "text" not in part.model_fields_set
                or part.filename is not None
                or part.mediaType not in {None, "", "text/plain"}
            ):
                _fail("CONTENT_TYPE_NOT_SUPPORTED")
            assert part.text is not None
            text_parts.append(part.text)
        combined = "\n".join(text_parts)
        _bounded_text(combined, "message text", maximum=24_576)
        configuration = request.configuration
        if configuration is not None:
            if configuration.taskPushNotificationConfig is not None:
                _fail("PUSH_NOT_SUPPORTED")
        normalized_params = request.model_dump(exclude_none=True)
        normalized_configuration = dict(normalized_params.get("configuration", {}))
        normalized_configuration["acceptedOutputModes"] = ["text/plain"]
        normalized_params["configuration"] = normalized_configuration
        return CoreMethodCall(
            method,
            _freeze(normalized_params),
            normalized_text=combined,
        )
    if method == "GetTask":
        request = _model(_GetTaskRequest, params)
        assert isinstance(request, _GetTaskRequest)
        return CoreMethodCall(
            method,
            _freeze(request.model_dump(exclude_none=True)),
        )
    if method == "ListTasks":
        request = _model(_ListTasksRequest, params)
        assert isinstance(request, _ListTasksRequest)
        normalized = request.model_dump(exclude_none=True)
        normalized["pageSize"] = (
            50 if request.pageSize is None else request.pageSize
        )
        return CoreMethodCall(method, _freeze(normalized))
    if method == "CancelTask":
        request = _model(_CancelTaskRequest, params)
        assert isinstance(request, _CancelTaskRequest)
        return CoreMethodCall(
            method,
            _freeze(request.model_dump(exclude_none=True)),
        )
    if method == "SubscribeToTask":
        request = _model(_SubscribeRequest, params)
        assert isinstance(request, _SubscribeRequest)
        return CoreMethodCall(
            method,
            _freeze(request.model_dump(exclude_none=True)),
        )
    if method == "CreateTaskPushNotificationConfig":
        _model(_TaskPushNotificationConfig, params)
        _fail("PUSH_NOT_SUPPORTED")
    if method in {
        "GetTaskPushNotificationConfig",
        "DeleteTaskPushNotificationConfig",
    }:
        _model(_PushItemRequest, params)
        _fail("PUSH_NOT_SUPPORTED")
    if method == "ListTaskPushNotificationConfigs":
        _model(_PushListRequest, params)
        _fail("PUSH_NOT_SUPPORTED")
    if method == "GetExtendedAgentCard":
        _model(_ExtendedCardRequest, params)
        _fail("UNSUPPORTED_OPERATION")
    raise AssertionError("recognized method is not routed")


def task_state_is_terminal(state: Any) -> bool:
    """Return whether an A2A Task state closes a streaming response."""

    return isinstance(state, str) and state in _TERMINAL_TASK_STATES


def utc_timestamp() -> str:
    """Return one canonical UTC ProtoJSON timestamp."""

    return datetime.now(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _protocol_timestamp() -> str:
    return utc_timestamp()


def _response_model(model: type[BaseModel], value: Any) -> BaseModel:
    try:
        return model.model_validate(_plain(value))
    except A2AError as error:
        raise A2AError("INVALID_AGENT_RESPONSE") from error
    except (ValidationError, ValueError, TypeError) as error:
        raise A2AError("INVALID_AGENT_RESPONSE") from error


def validate_task(
    value: Any,
    *,
    expected_task_id: str | None = None,
    expected_context_id: str | None = None,
) -> Mapping[str, Any]:
    """Validate and normalize one Task received from an authenticated peer."""

    task = _response_model(_Task, value)
    assert isinstance(task, _Task)
    if expected_task_id is not None:
        try:
            expected_task_id = _canonical_uuid4(expected_task_id)
        except A2AError as error:
            raise A2AError("INVALID_AGENT_RESPONSE") from error
        if task.id != expected_task_id:
            _fail("INVALID_AGENT_RESPONSE")
    if expected_context_id is not None:
        try:
            expected_context_id = _canonical_uuid4(expected_context_id)
        except A2AError as error:
            raise A2AError("INVALID_AGENT_RESPONSE") from error
        if task.contextId != expected_context_id:
            _fail("INVALID_AGENT_RESPONSE")
    return _freeze(task.model_dump(exclude_none=True))


def validate_send_task_result(
    result: Any,
    *,
    expected_task_id: str | None = None,
    expected_context_id: str | None = None,
) -> Mapping[str, Any]:
    """Validate the Task-only M-Claw profile of ``SendMessageResponse``."""

    response = _response_model(_SendMessageResponse, result)
    assert isinstance(response, _SendMessageResponse)
    if response.task is None or response.message is not None:
        _fail("INVALID_AGENT_RESPONSE")
    task = validate_task(
        response.task.model_dump(exclude_none=True),
        expected_task_id=expected_task_id,
        expected_context_id=expected_context_id,
    )
    return MappingProxyType({"task": task})


def validate_stream_response(
    result: Any,
    *,
    expected_task_id: str | None = None,
    expected_context_id: str | None = None,
) -> Mapping[str, Any]:
    """Validate one A2A 1.0 StreamResponse payload.

    M-Claw accepts all standard payload shapes at the protocol boundary, then
    binds Task events to the initial server-generated Task when expectations
    are supplied by the caller.
    """

    response = _response_model(_StreamResponse, result)
    assert isinstance(response, _StreamResponse)
    plain = response.model_dump(exclude_none=True)
    if response.task is not None:
        return MappingProxyType(
            {
                "task": validate_task(
                    plain["task"],
                    expected_task_id=expected_task_id,
                    expected_context_id=expected_context_id,
                )
            }
        )
    if response.message is not None:
        message = response.message
        if message.role != "ROLE_AGENT":
            _fail("INVALID_AGENT_RESPONSE")
        return _freeze({"message": plain["message"]})
    if response.statusUpdate is not None:
        update = response.statusUpdate
        if expected_task_id is not None and update.taskId != expected_task_id:
            _fail("INVALID_AGENT_RESPONSE")
        if expected_context_id is not None and update.contextId != expected_context_id:
            _fail("INVALID_AGENT_RESPONSE")
        return _freeze({"statusUpdate": plain["statusUpdate"]})
    assert response.artifactUpdate is not None
    update = response.artifactUpdate
    if expected_task_id is not None and update.taskId != expected_task_id:
        _fail("INVALID_AGENT_RESPONSE")
    if expected_context_id is not None and update.contextId != expected_context_id:
        _fail("INVALID_AGENT_RESPONSE")
    return _freeze({"artifactUpdate": plain["artifactUpdate"]})


def build_task(
    *,
    task_id: str,
    context_id: str,
    state: str,
    history: Sequence[Mapping[str, Any]] = (),
    artifacts: Sequence[Mapping[str, Any]] = (),
    status_message: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    timestamp: str | None = None,
) -> Mapping[str, Any]:
    """Build one bounded Task object for persistence and wire use."""

    status: dict[str, Any] = {
        "state": state,
        "timestamp": timestamp or _protocol_timestamp(),
    }
    if status_message is not None:
        status["message"] = _plain(status_message)
    value: dict[str, Any] = {
        "id": task_id,
        "contextId": context_id,
        "status": status,
        "artifacts": [_plain(item) for item in artifacts],
        "history": [_plain(item) for item in history],
    }
    if metadata is not None:
        value["metadata"] = _plain(metadata)
    task = _model(_Task, value)
    assert isinstance(task, _Task)
    return _freeze(task.model_dump(exclude_none=True))


def build_status_update(
    *,
    task_id: str,
    context_id: str,
    state: str,
    message: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    timestamp: str | None = None,
) -> Mapping[str, Any]:
    status: dict[str, Any] = {
        "state": state,
        "timestamp": timestamp or _protocol_timestamp(),
    }
    if message is not None:
        status["message"] = _plain(message)
    update: dict[str, Any] = {
        "taskId": task_id,
        "contextId": context_id,
        "status": status,
    }
    if metadata is not None:
        update["metadata"] = _plain(metadata)
    model = _model(_TaskStatusUpdateEvent, update)
    assert isinstance(model, _TaskStatusUpdateEvent)
    return _freeze({"statusUpdate": model.model_dump(exclude_none=True)})


def build_artifact_update(
    *,
    task_id: str,
    context_id: str,
    artifact: Mapping[str, Any],
    append: bool = False,
    last_chunk: bool = True,
    metadata: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    update: dict[str, Any] = {
        "taskId": task_id,
        "contextId": context_id,
        "artifact": _plain(artifact),
        "append": append,
        "lastChunk": last_chunk,
    }
    if metadata is not None:
        update["metadata"] = _plain(metadata)
    model = _model(_TaskArtifactUpdateEvent, update)
    assert isinstance(model, _TaskArtifactUpdateEvent)
    return _freeze({"artifactUpdate": model.model_dump(exclude_none=True)})


@dataclass(frozen=True, slots=True)
class AgentCard:
    document: Mapping[str, Any]
    canonical_bytes: bytes
    device_id: str
    agent_id: str


def parse_softbus_url(value: Any) -> str:
    if not isinstance(value, str) or _SOFTBUS_URL.fullmatch(value) is None:
        _fail("INVALID_REQUEST")
    return value


def build_agent_card(
    *,
    device_id: str,
    agent_id: str,
    provider_ready: bool,
    provider_readiness_code: str,
    mclaw_version: str = MCLAW_VERSION,
) -> AgentCard:
    device_match = _DEVICE_ID.fullmatch(device_id) if isinstance(device_id, str) else None
    agent_match = _AGENT_ID.fullmatch(agent_id) if isinstance(agent_id, str) else None
    if (
        device_match is None
        or agent_match is None
        or device_match.group(1) != agent_match.group(1)
    ):
        _fail("INVALID_REQUEST")
    if type(provider_ready) is not bool:
        raise TypeError("provider_ready must be bool")
    readiness_codes = frozenset(
        {
            "",
            "PROVIDER_MISSING",
            "TRANSPORT_FENCE_UNSUPPORTED",
            "PROVIDER_SYNC_FAILED",
        }
    )
    if (
        provider_readiness_code not in readiness_codes
        or provider_ready != (provider_readiness_code == "")
    ):
        _fail("INVALID_REQUEST")
    _bounded_text(mclaw_version, "version", maximum=128)
    digest = device_match.group(1)
    document = {
        "name": "M-Claw",
        "description": "M-Claw agent hosted on a trusted OpenHarmony device.",
        "supportedInterfaces": [
            {
                "url": f"softbus://{digest}/{protocol.SERVICE_NAME}",
                "protocolBinding": protocol.PROTOCOL_BINDING,
                "protocolVersion": protocol.A2A_PROTOCOL_VERSION,
            }
        ],
        "version": mclaw_version,
        "capabilities": {
            "streaming": True,
            "pushNotifications": False,
            "extendedAgentCard": False,
            "extensions": [
                {
                    "uri": protocol.DEVICE_CONTEXT_EXTENSION_URI,
                    "description": (
                        "Independent Device Manifest and Device State endpoints."
                    ),
                    "required": False,
                    "params": {
                        "agentId": agent_id,
                        "hostDeviceId": device_id,
                        "providerReady": provider_ready,
                        "providerReadinessCode": provider_readiness_code,
                        "manifestMethod": "mclaw.deviceManifest.get",
                        "stateMethod": "mclaw.deviceState.get",
                    },
                }
            ],
        },
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "mclaw.general",
                "name": "M-Claw general agent",
                "description": "Accept a text task and return a text result.",
                "tags": ["mclaw", "text", "device-agent"],
            }
        ],
    }
    return validate_agent_card(
        document,
        expected_device_id=device_id,
        expected_agent_id=agent_id,
        require_exact=True,
    )


def validate_agent_card(
    value: Any,
    *,
    expected_device_id: str,
    expected_agent_id: str,
    require_exact: bool = False,
) -> AgentCard:
    if not isinstance(value, dict):
        _fail("INVALID_REQUEST")
    device_match = (
        _DEVICE_ID.fullmatch(expected_device_id)
        if isinstance(expected_device_id, str)
        else None
    )
    agent_match = (
        _AGENT_ID.fullmatch(expected_agent_id)
        if isinstance(expected_agent_id, str)
        else None
    )
    if (
        device_match is None
        or agent_match is None
        or device_match.group(1) != agent_match.group(1)
    ):
        _fail("INVALID_REQUEST")
    card = _recognized(
        value,
        frozenset(
            {
                "name",
                "description",
                "supportedInterfaces",
                "version",
                "capabilities",
                "defaultInputModes",
                "defaultOutputModes",
                "skills",
            }
        ),
        require_exact=require_exact,
    )
    _bounded_text(card["name"], "name", maximum=128)
    _bounded_text(card["description"], "description", maximum=1_024)
    _bounded_text(card["version"], "version", maximum=128)
    if not isinstance(card["supportedInterfaces"], list) or len(
        card["supportedInterfaces"]
    ) != 1:
        _fail("INVALID_REQUEST")
    interface = _recognized(
        card["supportedInterfaces"][0],
        frozenset({"url", "protocolBinding", "protocolVersion"}),
        require_exact=require_exact,
    )
    url = parse_softbus_url(interface["url"])
    if (
        _SOFTBUS_URL.fullmatch(url).group(1) != device_match.group(1)  # type: ignore[union-attr]
        or interface["protocolBinding"] != protocol.PROTOCOL_BINDING
        or interface["protocolVersion"] != protocol.A2A_PROTOCOL_VERSION
    ):
        _fail("BINDING_INCOMPATIBLE")
    capabilities = _recognized(
        card["capabilities"],
        frozenset(
            {"streaming", "pushNotifications", "extendedAgentCard", "extensions"}
        ),
        require_exact=require_exact,
    )
    if (
        capabilities["streaming"] is not True
        or capabilities["pushNotifications"] is not False
        or capabilities["extendedAgentCard"] is not False
        or not isinstance(capabilities["extensions"], list)
        or len(capabilities["extensions"]) != 1
    ):
        _fail("BINDING_INCOMPATIBLE")
    extension = _recognized(
        capabilities["extensions"][0],
        frozenset({"uri", "description", "required", "params"}),
        require_exact=require_exact,
    )
    if (
        extension["uri"] != protocol.DEVICE_CONTEXT_EXTENSION_URI
        or extension["required"] is not False
    ):
        _fail("BINDING_INCOMPATIBLE")
    _bounded_text(extension["description"], "extension.description", maximum=1_024)
    params = _exact(
        extension["params"],
        frozenset(
            {
                "agentId",
                "hostDeviceId",
                "providerReady",
                "providerReadinessCode",
                "manifestMethod",
                "stateMethod",
            }
        ),
    )
    readiness_codes = frozenset(
        {
            "",
            "PROVIDER_MISSING",
            "TRANSPORT_FENCE_UNSUPPORTED",
            "PROVIDER_SYNC_FAILED",
        }
    )
    if (
        params["agentId"] != expected_agent_id
        or params["hostDeviceId"] != expected_device_id
        or type(params["providerReady"]) is not bool
        or params["providerReadinessCode"] not in readiness_codes
        or params["providerReady"] != (params["providerReadinessCode"] == "")
        or params["manifestMethod"] != "mclaw.deviceManifest.get"
        or params["stateMethod"] != "mclaw.deviceState.get"
    ):
        _fail("BINDING_INCOMPATIBLE")
    if card["defaultInputModes"] != ["text/plain"] or card[
        "defaultOutputModes"
    ] != ["text/plain"]:
        _fail("BINDING_INCOMPATIBLE")
    if not isinstance(card["skills"], list) or len(card["skills"]) != 1:
        _fail("INVALID_REQUEST")
    skill = _recognized(
        card["skills"][0],
        frozenset({"id", "name", "description", "tags"}),
        require_exact=require_exact,
    )
    if (
        skill["id"] != "mclaw.general"
        or not isinstance(skill["tags"], list)
        or skill["tags"] != ["mclaw", "text", "device-agent"]
    ):
        _fail("INVALID_REQUEST")
    _bounded_text(skill["name"], "skill.name", maximum=128)
    _bounded_text(skill["description"], "skill.description", maximum=1_024)
    extension = {**extension, "params": copy.deepcopy(params)}
    capabilities = {**capabilities, "extensions": [extension]}
    card = {
        **card,
        "supportedInterfaces": [copy.deepcopy(interface)],
        "capabilities": capabilities,
        "skills": [copy.deepcopy(skill)],
    }
    encoded = compact_json_bytes(card)
    if len(encoded) > protocol.AGENT_CARD_MAX:
        _fail("FRAME_TOO_LARGE")
    return AgentCard(_freeze(copy.deepcopy(card)), encoded, expected_device_id, expected_agent_id)


__all__ = [
    "A2AError",
    "AgentCard",
    "CoreMethodCall",
    "EncodedFrame",
    "ERROR_DOMAIN",
    "ERROR_INFO_TYPE",
    "PRIVATE_PROFILE_LABEL",
    "RequestEnvelope",
    "ResponseEnvelope",
    "ServiceParameters",
    "build_agent_card",
    "build_artifact_update",
    "build_rpc_error",
    "build_rpc_request",
    "build_rpc_success",
    "build_status_update",
    "build_task",
    "compact_json_bytes",
    "encode_error_frame",
    "encode_frame_or_error",
    "encode_request_frame",
    "encode_success_frame",
    "parse_request_frame",
    "parse_response_frame",
    "parse_softbus_url",
    "validate_agent_card",
    "validate_core_method",
    "validate_send_task_result",
    "validate_service_parameters",
    "validate_stream_response",
    "validate_task",
    "task_state_is_terminal",
    "utc_timestamp",
]
