# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Owner-confined SoftBus binding session and bounded frame scheduler.

The Worker owns Native callbacks and byte transport only.  This module owns the
authenticated peer binding, RPC request routing, Agent Card verification, and
all reservations that exist between the shared A2A encoder and ``send_bytes``.
It deliberately has no threads and must be driven by the Runtime owner loop.
"""

from __future__ import annotations

import copy
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, NoReturn

from . import protocol
from .a2a import (
    A2AError,
    AgentCard,
    RequestEnvelope,
    ResponseEnvelope,
    encode_error_frame,
    encode_request_frame,
    encode_success_frame,
    parse_request_frame,
    parse_response_frame,
    task_state_is_terminal,
    validate_agent_card,
    validate_core_method,
    validate_stream_response,
)
from .binding import (
    BindingError,
    BindingPhase,
    InMemoryBinding,
    LocalBindingIdentity,
    PeerBindingIdentity,
)

_QUEUE_BUSINESS = "business"
_QUEUE_CONTROL = "control"
_CONTROL_REASONS = frozenset(
    {
        "PARSE_ERROR",
        "INVALID_REQUEST",
        "CAPACITY_BUSY",
        "RUNTIME_STOPPING",
        "BINDING_INCOMPATIBLE",
    }
)


class SoftBusBindingError(RuntimeError):
    """Stable owner-side binding or send-scheduler failure."""

    def __init__(self, code: str, *, socket: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.socket = socket


def _fail(code: str, *, socket: int | None = None) -> NoReturn:
    raise SoftBusBindingError(code, socket=socket)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return copy.deepcopy(value)


@dataclass(frozen=True, slots=True)
class OutboundFrame:
    """One fully encoded and size-checked SoftBus payload."""

    data: bytes
    queue: str = _QUEUE_BUSINESS
    close_after_send: bool = False
    response_reservation_id: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.data, bytes)
            or not 1 <= len(self.data) <= protocol.REMOTE_FRAME_MAX
            or self.queue not in {_QUEUE_BUSINESS, _QUEUE_CONTROL}
            or type(self.close_after_send) is not bool
            or (
                self.response_reservation_id is not None
                and (
                    self.queue != _QUEUE_BUSINESS
                    or not isinstance(self.response_reservation_id, str)
                    or not self.response_reservation_id
                )
            )
        ):
            raise ValueError("outbound frame is invalid")


@dataclass(frozen=True, slots=True)
class BindingReceiveResult:
    """Result of processing exactly one inbound SoftBus Bytes payload."""

    outbound: tuple[OutboundFrame, ...] = ()
    application_request: RequestEnvelope | None = None
    application_response: ApplicationResponse | None = None
    close_generation: bool = False


@dataclass(frozen=True, slots=True)
class ApplicationRequest:
    """One locally initiated, generation-bound application request."""

    request_id: str
    method: str
    params: Mapping[str, Any]
    frame: OutboundFrame


@dataclass(frozen=True, slots=True)
class ApplicationResponse:
    """A routed response retaining the initiating method and parameters."""

    request_id: str
    method: str
    params: Mapping[str, Any]
    result: Any | None
    error_reason: str | None
    outcome_unknown: bool = False
    stream_end: bool = True


@dataclass(frozen=True, slots=True)
class _PendingRequest:
    method: str
    params: Mapping[str, Any]
    streaming: bool = False


class SoftBusA2ABinding:
    """Wire adapter around :class:`InMemoryBinding` for one Socket generation."""

    def __init__(
        self,
        *,
        local: LocalBindingIdentity,
        local_card: AgentCard | Mapping[str, Any],
        authenticated_peer_device_id: str,
        authenticated_peer_agent_id: str,
        initiator: bool,
        connection_generation: int,
        negotiated_mtu: int,
        nonce_factory: Callable[[int], str] | None = None,
        request_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if type(negotiated_mtu) is not int or negotiated_mtu < protocol.MIN_NEGOTIATED_FRAME:
            _fail("BINDING_INCOMPATIBLE")
        self._mtu = min(negotiated_mtu, protocol.REMOTE_FRAME_MAX)
        self._initiator = initiator
        self._request_id_factory = request_id_factory or (lambda: str(uuid.uuid4()))
        binding_kwargs: dict[str, Any] = {
            "local": local,
            "authenticated_peer_device_id": authenticated_peer_device_id,
            "authenticated_peer_agent_id": authenticated_peer_agent_id,
            "initiator": initiator,
            "connection_generation": connection_generation,
        }
        if nonce_factory is not None:
            binding_kwargs["nonce_factory"] = nonce_factory
        self._binding = InMemoryBinding(**binding_kwargs)
        try:
            card = (
                local_card
                if isinstance(local_card, AgentCard)
                else validate_agent_card(
                    _plain(local_card),
                    expected_device_id=local.device_id,
                    expected_agent_id=local.agent_id,
                )
            )
        except A2AError as error:
            raise SoftBusBindingError("BINDING_INPUT_INVALID") from error
        if card.device_id != local.device_id or card.agent_id != local.agent_id:
            _fail("BINDING_INPUT_INVALID")
        self._local_card = card
        self._pending: dict[str, _PendingRequest] = {}
        self._card_request_sent = False
        self._started = False

    @property
    def phase(self) -> str:
        return self._binding.phase

    @property
    def connection_generation(self) -> int:
        return self._binding.connection_generation

    @property
    def ready(self) -> bool:
        return self.phase == BindingPhase.READY

    @property
    def peer_card(self) -> AgentCard | None:
        return self._binding.peer_card

    @property
    def peer_identity(self) -> PeerBindingIdentity | None:
        return self._binding.peer_identity

    @property
    def response_reservation_bytes(self) -> int:
        """Return the exact worst-case response slot for this Socket."""

        return self._mtu

    def _next_request_id(self) -> str:
        request_id = self._request_id_factory()
        try:
            return protocol.canonical_uuid4(request_id, "rpc.id")
        except protocol.ProtocolError as error:
            self.close()
            raise SoftBusBindingError("BINDING_INPUT_INVALID") from error

    def _request(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        extensions: tuple[str, ...] = (),
        streaming: bool = False,
    ) -> ApplicationRequest:
        if self.phase == BindingPhase.CLOSED:
            _fail("STALE_GENERATION")
        request_id = self._next_request_id()
        if request_id in self._pending:
            self.close()
            _fail("BINDING_INCOMPATIBLE")
        try:
            encoded = encode_request_frame(
                method,
                params,
                negotiated_mtu=self._mtu,
                request_id=request_id,
                extensions=extensions,
                phase=self.phase,
            )
        except A2AError as error:
            if self.phase != BindingPhase.READY:
                self.close()
            raise SoftBusBindingError(error.reason) from error
        if encoded.data is None or encoded.close_generation:
            if self.phase != BindingPhase.READY:
                self.close()
            _fail(encoded.terminal_reason or "BINDING_INCOMPATIBLE")
        normalized_params = _plain(params)
        self._pending[request_id] = _PendingRequest(
            method,
            MappingProxyType(normalized_params),
            streaming,
        )
        return ApplicationRequest(
            request_id,
            method,
            MappingProxyType(copy.deepcopy(normalized_params)),
            OutboundFrame(
                encoded.data,
                queue=(
                    _QUEUE_CONTROL
                    if method == "CancelTask"
                    else _QUEUE_BUSINESS
                ),
            ),
        )

    def _success(
        self,
        request_id: str,
        result: Any,
        *,
        control: bool = False,
        response_reserved: bool = False,
    ) -> OutboundFrame:
        try:
            encoded = encode_success_frame(
                request_id,
                result,
                negotiated_mtu=self._mtu,
                phase=self.phase,
            )
        except A2AError as error:
            self.close()
            raise SoftBusBindingError(error.reason) from error
        if encoded.data is None:
            self.close()
            _fail(encoded.terminal_reason or "BINDING_INCOMPATIBLE")
        if encoded.close_generation:
            self.close()
        return OutboundFrame(
            encoded.data,
            queue=_QUEUE_CONTROL if control else _QUEUE_BUSINESS,
            close_after_send=encoded.close_generation,
            response_reservation_id=request_id if response_reserved else None,
        )

    def _error(
        self,
        request_id: str | None,
        reason: str,
        *,
        control: bool,
        phase_override: str | None = None,
        response_reserved: bool = False,
        outcome_unknown: bool = False,
    ) -> BindingReceiveResult:
        phase = self.phase if phase_override is None else phase_override
        try:
            encoded = encode_error_frame(
                request_id,
                reason,
                negotiated_mtu=self._mtu,
                phase=phase,
                outcome_unknown=outcome_unknown,
            )
        except (A2AError, ValueError) as error:
            self.close()
            raise SoftBusBindingError("BINDING_INCOMPATIBLE") from error
        if encoded.data is None:
            self.close()
            return BindingReceiveResult(close_generation=True)
        close_after_send = encoded.close_generation
        if close_after_send:
            self.close()
        queue = _QUEUE_CONTROL if control else _QUEUE_BUSINESS
        return BindingReceiveResult(
            outbound=(
                OutboundFrame(
                    encoded.data,
                    queue=queue,
                    close_after_send=close_after_send,
                    response_reservation_id=(
                        request_id if response_reserved else None
                    ),
                ),
            )
        )

    def _maybe_start_card(self) -> tuple[OutboundFrame, ...]:
        if self.phase != BindingPhase.BINDING_OPEN or self._card_request_sent:
            return ()
        try:
            self._binding.begin_card_verification()
            frame = self._request("mclaw.agentCard.get", {}).frame
        except BindingError as error:
            self.close()
            raise SoftBusBindingError(error.code) from error
        self._card_request_sent = True
        return (frame,)

    def start(self) -> tuple[OutboundFrame, ...]:
        """Start the handshake; only the deterministic initiator emits bytes."""

        if self._started:
            _fail("BINDING_INCOMPATIBLE")
        self._started = True
        if not self._initiator:
            return ()
        try:
            params = self._binding.make_local_open()
        except BindingError as error:
            self.close()
            raise SoftBusBindingError(error.code) from error
        return (self._request("mclaw.binding.open", params).frame,)

    def _classify(self, raw: bytes) -> tuple[str, str | None]:
        try:
            value = protocol.strict_json_loads(
                raw,
                max_bytes=self._mtu,
                require_object=True,
            )
        except protocol.ProtocolError as error:
            reason = "PARSE_ERROR" if error.code == "PARSE_ERROR" else "INVALID_REQUEST"
            return reason, None
        rpc = value.get("rpc") if isinstance(value, dict) else None
        request_id: str | None = None
        if isinstance(rpc, dict):
            try:
                request_id = protocol.canonical_uuid4(rpc.get("id"), "rpc.id")
            except protocol.ProtocolError:
                request_id = None
            if "method" in rpc or "params" in rpc:
                return "request", request_id
        return "response", request_id

    def _handle_request(self, request: RequestEnvelope) -> BindingReceiveResult:
        try:
            self._binding.require_method(request.method)
        except BindingError as error:
            return self._error(request.request_id, error.code, control=False)

        if request.method == "mclaw.binding.open":
            phase_before_open = self.phase
            try:
                result = self._binding.accept_peer_open(request.params)
                outbound: list[OutboundFrame] = [
                    self._success(request.request_id, _plain(result))
                ]
                if not self._initiator:
                    params = self._binding.make_local_open()
                    outbound.append(self._request("mclaw.binding.open", params).frame)
                outbound.extend(self._maybe_start_card())
                return BindingReceiveResult(outbound=tuple(outbound))
            except BindingError as error:
                return self._error(
                    request.request_id,
                    error.code,
                    control=False,
                    phase_override=phase_before_open,
                )

        if request.method == "mclaw.agentCard.get":
            try:
                validate_core_method(request.method, request.params)
                return BindingReceiveResult(
                    outbound=(
                        self._success(
                            request.request_id,
                            {"agentCard": _plain(self._local_card.document)},
                        ),
                    )
                )
            except A2AError as error:
                return self._error(request.request_id, error.reason, control=False)

        return BindingReceiveResult(application_request=request)

    def _handle_response(self, response: ResponseEnvelope) -> BindingReceiveResult:
        request_id = response.request_id
        if request_id is None or request_id not in self._pending:
            self.close()
            return BindingReceiveResult(close_generation=True)
        pending = self._pending[request_id]
        method = pending.method
        if response.error is not None:
            self._pending.pop(request_id, None)
            if method in {"mclaw.binding.open", "mclaw.agentCard.get"}:
                self.close()
                return BindingReceiveResult(close_generation=True)
            reason = str(response.error["data"][0]["reason"])
            outcome_unknown = (
                response.error["data"][0]["metadata"]["outcomeUnknown"] == "true"
            )
            return BindingReceiveResult(
                application_response=ApplicationResponse(
                    request_id,
                    method,
                    pending.params,
                    None,
                    reason,
                    outcome_unknown,
                    True,
                )
            )
        try:
            if method == "mclaw.binding.open":
                self._pending.pop(request_id, None)
                self._binding.accept_local_open_ack(_plain(response.result))
                return BindingReceiveResult(outbound=self._maybe_start_card())
            if method == "mclaw.agentCard.get":
                self._pending.pop(request_id, None)
                result = response.result
                if not isinstance(result, Mapping) or frozenset(result) != frozenset(
                    {"agentCard"}
                ):
                    raise BindingError("BINDING_INCOMPATIBLE")
                self._binding.complete_card_verification(
                    _plain(result["agentCard"])
                )
                return BindingReceiveResult()
        except BindingError:
            self.close()
            return BindingReceiveResult(close_generation=True)
        stream_end = True
        normalized_result = _plain(response.result)
        if pending.streaming:
            try:
                normalized_result = _plain(
                    validate_stream_response(normalized_result)
                )
            except A2AError:
                self.close()
                return BindingReceiveResult(close_generation=True)
            if "statusUpdate" in normalized_result:
                stream_end = task_state_is_terminal(
                    normalized_result["statusUpdate"]["status"]["state"]
                )
            elif "task" in normalized_result:
                stream_end = task_state_is_terminal(
                    normalized_result["task"]["status"]["state"]
                )
            else:
                stream_end = "message" in normalized_result
            if stream_end:
                self._pending.pop(request_id, None)
        else:
            self._pending.pop(request_id, None)
        return BindingReceiveResult(
            application_response=ApplicationResponse(
                request_id,
                method,
                pending.params,
                normalized_result,
                None,
                False,
                stream_end,
            )
        )

    def receive(self, raw: bytes) -> BindingReceiveResult:
        """Validate and route one complete inbound Bytes payload."""

        if self.phase == BindingPhase.CLOSED:
            _fail("STALE_GENERATION")
        classification, request_id_hint = self._classify(raw)
        if classification in {"PARSE_ERROR", "INVALID_REQUEST"}:
            return self._error(
                request_id_hint,
                classification,
                control=True,
            )
        if classification == "request":
            try:
                request = parse_request_frame(raw, negotiated_mtu=self._mtu)
            except A2AError as error:
                return self._error(
                    request_id_hint,
                    error.reason,
                    control=error.reason in _CONTROL_REASONS,
                )
            return self._handle_request(request)
        try:
            response = parse_response_frame(raw, negotiated_mtu=self._mtu)
        except A2AError:
            self.close()
            return BindingReceiveResult(close_generation=True)
        return self._handle_response(response)

    def reject_application_request(
        self, request: RequestEnvelope, reason: str = "PEER_NOT_READY"
    ) -> BindingReceiveResult:
        """Return a bounded terminal while a later service gate is unavailable."""

        if not isinstance(request, RequestEnvelope):
            raise TypeError("request must be RequestEnvelope")
        return self._error(
            request.request_id,
            reason,
            control=(
                request.method == "CancelTask"
                or reason in _CONTROL_REASONS
            ),
        )

    def complete_application_request(
        self,
        request: RequestEnvelope,
        *,
        result: Any | None = None,
        error_reason: str | None = None,
        outcome_unknown: bool = False,
    ) -> BindingReceiveResult:
        """Encode exactly one success or stable error for an inbound request."""

        if not isinstance(request, RequestEnvelope):
            raise TypeError("request must be RequestEnvelope")
        control = request.method == "CancelTask"
        if (
            type(outcome_unknown) is not bool
            or (error_reason is None) == (result is None)
            or (error_reason is None and outcome_unknown)
        ):
            raise ValueError("exactly one of result or error_reason is required")
        if error_reason is not None:
            return self._error(
                request.request_id,
                error_reason,
                control=control,
                response_reserved=not control,
                outcome_unknown=outcome_unknown,
            )
        return BindingReceiveResult(
            outbound=(
                self._success(
                    request.request_id,
                    _plain(result),
                    control=control,
                    response_reserved=not control,
                ),
            )
        )

    def complete_application_stream_event(
        self,
        request: RequestEnvelope,
        *,
        result: Mapping[str, Any],
        first: bool,
    ) -> BindingReceiveResult:
        """Encode one ordered success item for a SoftBus-backed A2A stream."""

        if not isinstance(request, RequestEnvelope):
            raise TypeError("request must be RequestEnvelope")
        if request.method not in {"SendStreamingMessage", "SubscribeToTask"}:
            raise ValueError("request is not a streaming operation")
        if type(first) is not bool or not isinstance(result, Mapping):
            raise ValueError("stream event is invalid")
        normalized = validate_stream_response(result)
        return BindingReceiveResult(
            outbound=(
                self._success(
                    request.request_id,
                    _plain(normalized),
                    response_reserved=first,
                ),
            )
        )

    def complete_application_stream_error(
        self,
        request: RequestEnvelope,
        *,
        error_reason: str,
        first: bool,
        outcome_unknown: bool = False,
    ) -> BindingReceiveResult:
        """Encode a terminal stream error without reusing its first-frame slot."""

        if not isinstance(request, RequestEnvelope):
            raise TypeError("request must be RequestEnvelope")
        if request.method not in {"SendStreamingMessage", "SubscribeToTask"}:
            raise ValueError("request is not a streaming operation")
        if type(first) is not bool:
            raise ValueError("stream position is invalid")
        return self._error(
            request.request_id,
            error_reason,
            control=False,
            response_reserved=first,
            outcome_unknown=outcome_unknown,
        )

    def request_application(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        extensions: tuple[str, ...] = (),
    ) -> ApplicationRequest:
        """Validate and encode a local request without weakening the binding."""

        if not self.ready:
            _fail("PEER_NOT_READY")
        try:
            call = validate_core_method(method, params)
        except A2AError as error:
            raise SoftBusBindingError(error.reason) from error
        normalized = getattr(call, "params", None)
        if not isinstance(normalized, Mapping):
            _fail("INVALID_PARAMS")
        return self._request(
            method,
            normalized,
            extensions=extensions,
            streaming=method in {"SendStreamingMessage", "SubscribeToTask"},
        )

    def device_context_extension_allowed(self, request: RequestEnvelope) -> bool:
        """Check the per-request extension gate after both Cards are verified."""

        return (
            isinstance(request, RequestEnvelope)
            and self.ready
            and self._binding.peer_card is not None
            and request.service_parameters.advertises(
                protocol.DEVICE_CONTEXT_EXTENSION_URI
            )
        )

    def close(self) -> None:
        self._pending.clear()
        self._binding.close()

    def public_snapshot(self) -> Mapping[str, Any]:
        value = dict(self._binding.public_snapshot())
        value.update(
            {
                "pendingRequestCount": len(self._pending),
                "peerCardVerified": self._binding.peer_card is not None,
            }
        )
        return MappingProxyType(value)


@dataclass(frozen=True, slots=True)
class SendCompletion:
    socket: int
    generation: int
    close_after_send: bool
    sent_bytes: int


@dataclass(slots=True)
class _SocketQueue:
    generation: int
    business: deque[OutboundFrame] = field(default_factory=deque)
    control: deque[OutboundFrame] = field(default_factory=deque)
    business_count: int = 0
    business_bytes: int = 0
    control_count: int = 0
    control_bytes: int = 0
    business_streak: int = 0
    control_streak: int = 0
    response_frames: set[str] = field(default_factory=set)
    response_reservations: dict[str, int] = field(default_factory=dict)


class SoftBusSendScheduler:
    """Bounded per-generation queues with one synchronous send in flight."""

    def __init__(self) -> None:
        self._sockets: dict[int, _SocketQueue] = {}
        self._active: deque[int] = deque()
        self._active_set: set[int] = set()
        self._business_count = 0
        self._business_bytes = 0
        self._business_admission_open = True
        self._overflow_count = 0

    @staticmethod
    def _validate_identity(socket: int, generation: int) -> None:
        if (
            type(socket) is not int
            or not 0 <= socket <= 2**31 - 1
            or type(generation) is not int
            or not 1 <= generation <= 2**63 - 1
        ):
            _fail("BINDING_INPUT_INVALID", socket=socket if type(socket) is int else None)

    def register(self, socket: int, generation: int) -> None:
        self._validate_identity(socket, generation)
        existing = self._sockets.get(socket)
        if existing is not None:
            if existing.generation == generation:
                return
            _fail("STALE_GENERATION", socket=socket)
        self._sockets[socket] = _SocketQueue(generation=generation)

    def _activate(self, socket: int) -> None:
        if socket not in self._active_set:
            self._active.append(socket)
            self._active_set.add(socket)

    def reserve_response(
        self,
        socket: int,
        generation: int,
        response_id: str,
        reserved_bytes: int,
    ) -> None:
        """Reserve one full response route before invoking an RPC handler."""

        self._validate_identity(socket, generation)
        if not isinstance(response_id, str) or not response_id:
            _fail("BINDING_INPUT_INVALID", socket=socket)
        if (
            type(reserved_bytes) is not int
            or not 1 <= reserved_bytes <= protocol.REMOTE_FRAME_MAX
        ):
            _fail("BINDING_INPUT_INVALID", socket=socket)
        state = self._sockets.get(socket)
        if state is None or state.generation != generation:
            _fail("STALE_GENERATION", socket=socket)
        if response_id in state.response_reservations:
            _fail("BINDING_INCOMPATIBLE", socket=socket)
        if not self._business_admission_open:
            _fail("RUNTIME_STOPPING", socket=socket)
        if (
            state.business_count >= protocol.SOCKET_SEND_QUEUE_MAX
            or state.business_bytes
            > protocol.SOCKET_SEND_QUEUE_BYTES_MAX - reserved_bytes
            or self._business_count >= protocol.GLOBAL_SEND_QUEUE_MAX
            or self._business_bytes
            > protocol.GLOBAL_SEND_QUEUE_BYTES_MAX - reserved_bytes
        ):
            self._overflow_count += 1
            _fail("CAPACITY_BUSY", socket=socket)
        state.response_reservations[response_id] = reserved_bytes
        state.business_count += 1
        state.business_bytes += reserved_bytes
        self._business_count += 1
        self._business_bytes += reserved_bytes

    def cancel_response_reservation(
        self, socket: int, generation: int, response_id: str
    ) -> bool:
        """Release an unqueued route when service admission cannot continue."""

        self._validate_identity(socket, generation)
        state = self._sockets.get(socket)
        if state is None:
            return False
        if state.generation != generation:
            _fail("STALE_GENERATION", socket=socket)
        if response_id in state.response_frames:
            _fail("BINDING_INCOMPATIBLE", socket=socket)
        reserved_bytes = state.response_reservations.pop(response_id, None)
        if reserved_bytes is None:
            return False
        state.business_count -= 1
        state.business_bytes -= reserved_bytes
        self._business_count -= 1
        self._business_bytes -= reserved_bytes
        if min(
            state.business_count,
            state.business_bytes,
            self._business_count,
            self._business_bytes,
        ) < 0:
            raise AssertionError("response reservation underflow")
        return True

    def enqueue(self, socket: int, generation: int, frame: OutboundFrame) -> None:
        self._validate_identity(socket, generation)
        if not isinstance(frame, OutboundFrame):
            raise TypeError("frame must be OutboundFrame")
        state = self._sockets.get(socket)
        if state is None or state.generation != generation:
            _fail("STALE_GENERATION", socket=socket)
        size = len(frame.data)
        if frame.queue == _QUEUE_BUSINESS:
            response_id = frame.response_reservation_id
            if response_id is not None:
                reserved_bytes = state.response_reservations.get(response_id)
                if (
                    reserved_bytes is None
                    or response_id in state.response_frames
                    or size > reserved_bytes
                ):
                    _fail("BINDING_INCOMPATIBLE", socket=socket)
                state.response_frames.add(response_id)
                state.business.append(frame)
                self._activate(socket)
                return
            if not self._business_admission_open:
                _fail("RUNTIME_STOPPING", socket=socket)
            if (
                state.business_count >= protocol.SOCKET_SEND_QUEUE_MAX
                or state.business_bytes > protocol.SOCKET_SEND_QUEUE_BYTES_MAX - size
                or self._business_count >= protocol.GLOBAL_SEND_QUEUE_MAX
                or self._business_bytes > protocol.GLOBAL_SEND_QUEUE_BYTES_MAX - size
            ):
                self._overflow_count += 1
                _fail("CAPACITY_BUSY", socket=socket)
            state.business.append(frame)
            state.business_count += 1
            state.business_bytes += size
            self._business_count += 1
            self._business_bytes += size
        else:
            if (
                state.control_count >= protocol.SOCKET_CONTROL_SEND_MAX
                or state.control_bytes > protocol.SOCKET_CONTROL_SEND_BYTES_MAX - size
            ):
                self._overflow_count += 1
                _fail("CONTROL_SEND_CAPACITY_FATAL", socket=socket)
            state.control.append(frame)
            state.control_count += 1
            state.control_bytes += size
        self._activate(socket)

    @staticmethod
    def _choose(state: _SocketQueue) -> OutboundFrame:
        if state.business and state.control:
            if state.business_streak >= protocol.SOCKET_BUSINESS_SEND_BURST_MAX:
                frame = state.control.popleft()
            elif state.control_streak >= protocol.SOCKET_CONTROL_SEND_BURST_MAX:
                frame = state.business.popleft()
            else:
                frame = state.business.popleft()
        elif state.business:
            frame = state.business.popleft()
        elif state.control:
            frame = state.control.popleft()
        else:
            raise AssertionError("active Socket has no queued frame")
        if frame.queue == _QUEUE_BUSINESS:
            state.business_streak += 1
            state.control_streak = 0
        else:
            state.control_streak += 1
            state.business_streak = 0
        return frame

    def _release(self, state: _SocketQueue, frame: OutboundFrame) -> None:
        size = len(frame.data)
        if frame.queue == _QUEUE_BUSINESS:
            response_id = frame.response_reservation_id
            if response_id is not None:
                reserved_bytes = state.response_reservations.pop(response_id, None)
                if (
                    reserved_bytes is None
                    or response_id not in state.response_frames
                ):
                    raise AssertionError("response reservation missing")
                state.response_frames.remove(response_id)
                size = reserved_bytes
            state.business_count -= 1
            state.business_bytes -= size
            self._business_count -= 1
            self._business_bytes -= size
        else:
            state.control_count -= 1
            state.control_bytes -= size
        if min(
            state.business_count,
            state.business_bytes,
            state.control_count,
            state.control_bytes,
            self._business_count,
            self._business_bytes,
        ) < 0:
            raise AssertionError("send reservation underflow")

    def drain_one(
        self, send: Callable[[int, bytes], int]
    ) -> SendCompletion | None:
        while self._active:
            socket = self._active.popleft()
            self._active_set.discard(socket)
            state = self._sockets.get(socket)
            if state is None or not (state.business or state.control):
                continue
            frame = self._choose(state)
            try:
                sent = send(socket, frame.data)
            except BaseException as error:
                self.unregister(socket, state.generation)
                raise SoftBusBindingError("SEND_BYTES_FAILED", socket=socket) from error
            if type(sent) is not int or sent != len(frame.data):
                self.unregister(socket, state.generation)
                _fail("SEND_BYTES_LENGTH_MISMATCH", socket=socket)
            self._release(state, frame)
            generation = state.generation
            if frame.close_after_send:
                self.unregister(socket, generation)
            elif state.business or state.control:
                self._activate(socket)
            return SendCompletion(socket, generation, frame.close_after_send, sent)
        return None

    def unregister(self, socket: int, generation: int) -> bool:
        self._validate_identity(socket, generation)
        state = self._sockets.get(socket)
        if state is None:
            return False
        if state.generation != generation:
            _fail("STALE_GENERATION", socket=socket)
        self._business_count -= state.business_count
        self._business_bytes -= state.business_bytes
        del self._sockets[socket]
        if socket in self._active_set:
            self._active_set.remove(socket)
            self._active = deque(item for item in self._active if item != socket)
        if min(self._business_count, self._business_bytes) < 0:
            raise AssertionError("global send reservation underflow")
        return True

    def begin_shutdown(self) -> None:
        self._business_admission_open = False

    def clear(self) -> None:
        self._sockets.clear()
        self._active.clear()
        self._active_set.clear()
        self._business_count = 0
        self._business_bytes = 0

    def diagnostic_snapshot(self) -> Mapping[str, int]:
        control_count = sum(state.control_count for state in self._sockets.values())
        control_bytes = sum(state.control_bytes for state in self._sockets.values())
        return MappingProxyType(
            {
                "sendBusinessQueueBytes": self._business_bytes,
                "sendBusinessQueueCount": self._business_count,
                "sendControlQueueBytes": control_bytes,
                "sendControlQueueCount": control_count,
                "sendQueueOverflowCount": self._overflow_count,
            }
        )


__all__ = [
    "ApplicationRequest",
    "ApplicationResponse",
    "BindingReceiveResult",
    "OutboundFrame",
    "SendCompletion",
    "SoftBusA2ABinding",
    "SoftBusBindingError",
    "SoftBusSendScheduler",
]
