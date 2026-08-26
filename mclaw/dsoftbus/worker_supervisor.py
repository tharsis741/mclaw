# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Bounded parent-side supervision for the isolated DSoftBus Worker.

The owner loop is the supervisor.  Per Worker epoch it owns exactly one stdin
writer, stdout reader, stderr reader, and process waiter thread.  This module
opens only ``hello`` and ``stop``; Native discovery and Socket operations stay
closed until the Runtime has frozen its public Manifest identity.  The owner
then opens the remaining operations through explicit, typed methods; arbitrary
Worker commands are never exposed to product callers.
"""

from __future__ import annotations

import base64
from collections import deque
from dataclasses import dataclass, field
import hashlib
import logging
import os
from pathlib import Path
import subprocess
import threading
import time
from types import MappingProxyType
from typing import Any, BinaryIO, Callable, Mapping, NoReturn, Protocol
import uuid

from . import protocol
from .baseline import (
    RUNTIME_PROFILE_FILENAME,
    RuntimeProfile,
)
from .health import worker_epoch_digest
from .presence import derive_public_device_id
from .worker_ipc import ParentStdinWriter, WorkerIpcFailure, WorkerIpcState


logger = logging.getLogger(__name__)


_HEX64 = frozenset("0123456789abcdef")
_TOKEN_DOMAIN = b"mclaw-dsoftbus-token-id\0"
_THREAD_JOIN_GRACE_S = 1.0


class WorkerSupervisorError(RuntimeError):
    """Stable, non-sensitive Worker supervision failure."""

    def __init__(
        self,
        code: str,
        *,
        outcome_unknown: bool = False,
        native_code: int | None = None,
        phase: str = "",
    ) -> None:
        super().__init__(code)
        self.code = code
        self.outcome_unknown = outcome_unknown
        self.native_code = native_code
        self.phase = phase


def _fail(
    code: str,
    *,
    outcome_unknown: bool = False,
    native_code: int | None = None,
    phase: str = "",
) -> NoReturn:
    raise WorkerSupervisorError(
        code,
        outcome_unknown=outcome_unknown,
        native_code=native_code,
        phase=phase,
    )


def _is_hex64(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _HEX64 for character in value)
    )


def _read_process_start_ticks(pid: int) -> int:
    """Read Linux/OpenHarmony proc start ticks without accepting PID reuse."""

    if type(pid) is not int or not 1 <= pid <= 2**31 - 1:
        _fail("WORKER_PROCESS_IDENTITY_INVALID")
    path = Path(f"/proc/{pid}/stat")
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise WorkerSupervisorError("WORKER_PROCESS_IDENTITY_INVALID") from error
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    if not raw or len(raw) > 65_536 or b"\n" in raw or b"\r" in raw or b"\0" in raw:
        _fail("WORKER_PROCESS_IDENTITY_INVALID")
    closing = raw.rfind(b")")
    if closing <= 0 or closing + 2 >= len(raw):
        _fail("WORKER_PROCESS_IDENTITY_INVALID")
    fields = raw[closing + 2 :].split()
    if len(fields) < 20:
        _fail("WORKER_PROCESS_IDENTITY_INVALID")
    try:
        start_ticks = int(fields[19], 10)
    except ValueError as error:
        raise WorkerSupervisorError("WORKER_PROCESS_IDENTITY_INVALID") from error
    if not 1 <= start_ticks <= 2**63 - 1:
        _fail("WORKER_PROCESS_IDENTITY_INVALID")
    return start_ticks


@dataclass(frozen=True, slots=True)
class WorkerIdentityExpectation:
    uid: int
    gid: int
    supplementary_gids: tuple[int, ...]
    capability_set: tuple[str, ...]
    token_id_hash: str
    selinux_domain: str
    socket_cap: int
    expected_device_id: str | None = None

    @classmethod
    def from_profile(
        cls,
        profile: RuntimeProfile,
        *,
        expected_device_id: str | None = None,
    ) -> WorkerIdentityExpectation:
        if not isinstance(profile, RuntimeProfile):
            raise TypeError("profile must be a Runtime Profile")
        closure = profile.document["runtimeClosure"]
        identity = closure["identity"]
        return cls(
            uid=identity["uid"],
            gid=identity["gid"],
            supplementary_gids=tuple(identity["supplementaryGids"]),
            capability_set=tuple(identity["capabilitySet"]),
            token_id_hash=identity["sealedTokenIdHash"],
            selinux_domain=identity["selinuxContext"],
            socket_cap=closure["softbusSocketCap"],
            expected_device_id=expected_device_id,
        ).validated()

    def validated(self) -> WorkerIdentityExpectation:
        if (
            type(self.uid) is not int
            or not 0 <= self.uid <= 2**32 - 1
            or type(self.gid) is not int
            or not 0 <= self.gid <= 2**32 - 1
            or not isinstance(self.supplementary_gids, tuple)
            or tuple(sorted(set(self.supplementary_gids))) != self.supplementary_gids
            or any(type(value) is not int or not 0 <= value <= 2**32 - 1 for value in self.supplementary_gids)
            or not isinstance(self.capability_set, tuple)
            or len(self.capability_set) != len(set(self.capability_set))
            or any(
                not isinstance(value, str)
                or not 1 <= len(value.encode("utf-8")) <= 64
                for value in self.capability_set
            )
            or not isinstance(self.token_id_hash, str)
            or not self.token_id_hash.startswith("sha256:")
            or not _is_hex64(self.token_id_hash[7:])
            or not isinstance(self.selinux_domain, str)
            or not 1 <= len(self.selinux_domain.encode("utf-8")) <= 256
            or type(self.socket_cap) is not int
            or not 4 <= self.socket_cap <= 2**32 - 1
            or (
                self.expected_device_id is not None
                and (
                    not isinstance(self.expected_device_id, str)
                    or not self.expected_device_id.startswith("urn:mclaw:device:oh:")
                    or not _is_hex64(self.expected_device_id.rsplit(":", 1)[-1])
                )
            )
        ):
            _fail("WORKER_IDENTITY_POLICY_INVALID")
        return self


@dataclass(frozen=True, slots=True)
class VerifiedWorkerIdentity:
    public_device_id: str
    public_agent_id: str
    worker_epoch: str
    pid: int
    start_time_ticks: int
    socket_cap: int
    local_udid: str = field(repr=False)


class WorkerProcessHandle(Protocol):
    pid: int
    start_time_ticks: int
    stdin: BinaryIO
    stdout: BinaryIO
    stderr: BinaryIO

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


class WorkerLauncher(Protocol):
    def spawn(self) -> WorkerProcessHandle: ...


class _SubprocessHandle:
    def __init__(
        self,
        process: subprocess.Popen[bytes],
        *,
        start_time_ticks: int,
    ) -> None:
        if process.stdin is None or process.stdout is None or process.stderr is None:
            _fail("WORKER_LAUNCH_FAILED")
        self._process = process
        self.pid = process.pid
        self.start_time_ticks = start_time_ticks
        self.stdin = process.stdin
        self.stdout = process.stdout
        self.stderr = process.stderr

    def poll(self) -> int | None:
        return self._process.poll()

    def terminate(self) -> None:
        self._process.terminate()

    def kill(self) -> None:
        self._process.kill()

    def wait(self, timeout: float | None = None) -> int:
        return self._process.wait(timeout=timeout)


class SubprocessWorkerLauncher:
    """Launch an already validated absolute argv with no shell."""

    def __init__(
        self,
        *,
        argv: tuple[str, ...],
        environment: Mapping[str, str],
        start_time_reader: Callable[[int], int] = _read_process_start_ticks,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    ) -> None:
        if (
            not isinstance(argv, tuple)
            or not argv
            or not isinstance(argv[0], str)
            or not argv[0]
            or not Path(argv[0]).is_absolute()
            or any(not isinstance(value, str) or not value or "\0" in value for value in argv)
            or not isinstance(environment, Mapping)
            or any(
                not isinstance(key, str)
                or not key
                or "=" in key
                or "\0" in key
                or not isinstance(value, str)
                or "\0" in value
                for key, value in environment.items()
            )
        ):
            _fail("WORKER_LAUNCH_CONFIGURATION_INVALID")
        self._argv = tuple(argv)
        self._environment = dict(environment)
        self._start_time_reader = start_time_reader
        self._popen = popen

    def spawn(self) -> WorkerProcessHandle:
        try:
            process = self._popen(
                list(self._argv),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=dict(self._environment),
                shell=False,
                bufsize=0,
                close_fds=True,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise WorkerSupervisorError("WORKER_LAUNCH_FAILED") from error
        try:
            ticks = self._start_time_reader(process.pid)
            if type(ticks) is not int or not 1 <= ticks <= 2**63 - 1:
                _fail("WORKER_PROCESS_IDENTITY_INVALID")
            return _SubprocessHandle(process, start_time_ticks=ticks)
        except BaseException:
            try:
                process.terminate()
            except BaseException:
                pass
            try:
                process.kill()
            except BaseException:
                pass
            try:
                process.wait(timeout=_THREAD_JOIN_GRACE_S)
            except BaseException:
                pass
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except BaseException:
                        pass
            raise


class ProfileWorkerLauncher(SubprocessWorkerLauncher):
    """Build the isolated Worker launch directly from one strict Profile."""

    def __init__(
        self,
        *,
        profile: RuntimeProfile,
        raw_token_id: str,
        expected_boot_id: str | None = None,
        start_time_reader: Callable[[int], int] = _read_process_start_ticks,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    ) -> None:
        if not isinstance(profile, RuntimeProfile):
            raise TypeError("profile must be a Runtime Profile")
        if (
            not isinstance(raw_token_id, str)
            or not raw_token_id.isascii()
            or not raw_token_id.isdecimal()
            or str(int(raw_token_id, 10)) != raw_token_id
            or not 1 <= int(raw_token_id, 10) <= 2**64 - 1
        ):
            _fail("WORKER_TOKEN_INPUT_INVALID")
        closure = profile.document["runtimeClosure"]
        identity = closure["identity"]
        token_hash = "sha256:" + hashlib.sha256(
            _TOKEN_DOMAIN + raw_token_id.encode("ascii")
        ).hexdigest()
        if token_hash != identity["sealedTokenIdHash"]:
            _fail("WORKER_TOKEN_INPUT_INVALID")
        selected_boot_id = expected_boot_id
        try:
            parsed_boot_id = uuid.UUID(selected_boot_id)
        except (AttributeError, TypeError, ValueError) as error:
            raise WorkerSupervisorError(
                "WORKER_LAUNCH_CONFIGURATION_INVALID"
            ) from error
        if str(parsed_boot_id) != selected_boot_id:
            _fail("WORKER_LAUNCH_CONFIGURATION_INVALID")
        python = closure["python"]
        deployment = python["deployment"]
        libraries = closure["libraries"]
        loader_path = ":".join(
            (
                str(Path(libraries["systemLibcxx"]["path"]).parent),
                str(Path(libraries["shim"]["path"]).parent),
                *python["releaseLibraryDirs"],
            )
        )
        profile_path = str(profile.path)
        if not Path(profile_path).is_absolute() or Path(profile_path).name != RUNTIME_PROFILE_FILENAME:
            _fail("WORKER_LAUNCH_CONFIGURATION_INVALID")
        manifest = deployment["manifest"]
        argv = (
            identity["launcher"]["path"],
            "--expected-boot-id",
            selected_boot_id,
            "--exec",
            "--token-id",
            raw_token_id,
            "--",
            python["executable"]["path"],
            "-I",
            "-S",
            deployment["workerBootstrapFile"],
        )
        environment = {
            "LD_LIBRARY_PATH": loader_path,
            "MCLAW_DSOFTBUS_EXPECTED_PROFILE_SHA256": profile.sha256,
            "MCLAW_DSOFTBUS_PROBE_PID": "1",
            "MCLAW_DSOFTBUS_PROFILE": profile_path,
            "MCLAW_DSOFTBUS_TOKEN_ID_HASH": token_hash,
            "MCLAW_DSOFTBUS_TOKEN_PROCESS_NAME": identity["processName"],
            "MCLAW_DSOFTBUS_WORKER_MANIFEST": manifest["path"],
            "MCLAW_DSOFTBUS_WORKER_MANIFEST_SHA256": manifest["sha256"],
        }
        super().__init__(
            argv=argv,
            environment=environment,
            start_time_reader=start_time_reader,
            popen=popen,
        )


@dataclass(frozen=True, slots=True)
class _Readiness:
    pid: int
    maps_sha256: str
    worker_epoch: str


@dataclass(slots=True)
class _ResponseWaiter:
    response: Mapping[str, Any] | None = None
    error_code: str = ""
    outcome_unknown: bool = False


@dataclass(slots=True)
class _Epoch:
    handle: WorkerProcessHandle
    readiness_event: threading.Event = field(default_factory=threading.Event)
    state_ready_event: threading.Event = field(default_factory=threading.Event)
    exit_event: threading.Event = field(default_factory=threading.Event)
    io_stop: threading.Event = field(default_factory=threading.Event)
    readiness: _Readiness | None = None
    ipc: WorkerIpcState | None = None
    waiters: dict[str, _ResponseWaiter] = field(default_factory=dict)
    threads: dict[str, threading.Thread] = field(default_factory=dict)
    failure_code: str = ""
    worker_diagnostic_code: str = ""
    event_overflow_counted: bool = False
    exit_code: int | None = None
    stopping: bool = False


def _read_bounded_line(stream: BinaryIO) -> bytes | None:
    try:
        raw = stream.readline(protocol.IPC_LINE_MAX + 1)
    except (OSError, ValueError) as error:
        raise WorkerSupervisorError("WORKER_STREAM_FAILED") from error
    if raw == b"":
        return None
    if len(raw) > protocol.IPC_LINE_MAX or not raw.endswith(b"\n"):
        _fail("WORKER_PROTOCOL_ERROR")
    return raw


def _parse_readiness(raw: bytes, expected_pid: int) -> _Readiness:
    try:
        value = protocol.strict_json_loads(
            raw,
            max_bytes=protocol.IPC_LINE_MAX,
            require_canonical=True,
            require_object=True,
        )
        diagnostic = protocol.exact_object(
            value,
            frozenset({"code", "kind", "mapsSha256", "pid", "workerEpoch"}),
            "worker readiness",
        )
        if diagnostic["code"] != "WORKER_READY" or diagnostic["kind"] != "worker-diagnostic":
            raise protocol.ProtocolError("INVALID_REQUEST", "readiness code invalid")
        pid = protocol.bounded_integer(
            diagnostic["pid"], "readiness.pid", 1, 2**31 - 1
        )
        if pid != expected_pid or not _is_hex64(diagnostic["mapsSha256"]):
            raise protocol.ProtocolError("INVALID_REQUEST", "readiness identity invalid")
        epoch = protocol.canonical_uuid4(diagnostic["workerEpoch"], "workerEpoch")
    except protocol.ProtocolError as error:
        raise WorkerSupervisorError("WORKER_PROTOCOL_ERROR") from error
    return _Readiness(pid=pid, maps_sha256=diagnostic["mapsSha256"], worker_epoch=epoch)


def _parse_startup_diagnostic(raw: bytes) -> str:
    return (
        "WORKER_START_FAILED"
        if _parse_worker_diagnostic_code(raw)
        else "WORKER_PROTOCOL_ERROR"
    )


def _parse_worker_diagnostic_code(raw: bytes) -> str:
    try:
        value = protocol.strict_json_loads(
            raw,
            max_bytes=protocol.IPC_LINE_MAX,
            require_canonical=True,
            require_object=True,
        )
        diagnostic = protocol.exact_object(
            value, frozenset({"code", "kind"}), "worker diagnostic"
        )
        code = diagnostic["code"]
        if (
            diagnostic["kind"] != "worker-diagnostic"
            or not isinstance(code, str)
            or not 1 <= len(code) <= 64
            or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ_" for character in code)
        ):
            raise protocol.ProtocolError("INVALID_REQUEST", "diagnostic invalid")
    except protocol.ProtocolError:
        return ""
    return code


class WorkerSupervisor:
    """Owner-confined lifecycle with bounded cross-thread IPC bookkeeping."""

    def __init__(
        self,
        *,
        launcher: WorkerLauncher,
        identity: WorkerIdentityExpectation,
        monotonic: Callable[[], float] = time.monotonic,
        control_timeout: float = protocol.CONTROL_TIMEOUT_S,
        thread_factory: Callable[..., threading.Thread] = threading.Thread,
    ) -> None:
        if not callable(getattr(launcher, "spawn", None)):
            raise TypeError("launcher must provide spawn()")
        if isinstance(control_timeout, bool) or not isinstance(control_timeout, (int, float)):
            raise TypeError("control_timeout must be a positive number")
        normalized_timeout = float(control_timeout)
        if not 0 < normalized_timeout <= protocol.DSOFTBUS_SHUTDOWN_TIMEOUT_S:
            raise ValueError("control_timeout must be within the shutdown bound")
        self._launcher = launcher
        self._identity = identity.validated()
        self._monotonic = monotonic
        self._control_timeout = normalized_timeout
        self._thread_factory = thread_factory
        self._condition = threading.Condition(threading.RLock())
        self._owner_thread_id: int | None = None
        self._current: _Epoch | None = None
        self._verified: VerifiedWorkerIdentity | None = None
        self._last_pid: int | None = None
        self._last_start_ticks: int | None = None
        self._last_epoch_digest: str | None = None
        self._last_failure_code = ""
        self._last_worker_diagnostic_code = ""
        self._last_exit_code: int | None = None
        self._first_start_attempted = False
        self._recovery_enabled = False
        self._restart_times: deque[float] = deque()
        self._restart_count = 0
        self._restart_exhausted = False
        self._event_overflow_count = 0
        self._closed = False
        self._publication_gate: Mapping[str, Any] | None = None
        self._events_enabled = False
        self._native_started_epoch: str | None = None
        self._operation_counts = {
            operation: 0 for operation in protocol.WORKER_OPERATIONS
        }

    def _require_owner(self, *, establish: bool = False) -> None:
        current = threading.get_ident()
        with self._condition:
            if self._owner_thread_id is None and establish:
                self._owner_thread_id = current
            if self._owner_thread_id != current:
                _fail("WORKER_SUPERVISOR_OWNER_MISMATCH")

    def _mark_failure(self, epoch: _Epoch, code: str) -> None:
        with self._condition:
            first_failure = not epoch.failure_code
            if (
                code == "PARENT_EVENT_CAPACITY_FATAL"
                and not epoch.event_overflow_counted
            ):
                epoch.event_overflow_counted = True
                self._event_overflow_count += 1
            if first_failure:
                epoch.failure_code = code
                self._last_failure_code = code
                self._last_worker_diagnostic_code = epoch.worker_diagnostic_code
                logger.warning(
                    "[DSOFTBUS_WORKER] failed pid=%s code=%s "
                    "workerDiagnosticCode=%s exitCode=%s",
                    epoch.handle.pid,
                    code,
                    epoch.worker_diagnostic_code or "none",
                    epoch.exit_code if epoch.exit_code is not None else "pending",
                )
            ipc = epoch.ipc
            if ipc is not None:
                ipc.fail_epoch()
            for command_id, waiter in tuple(epoch.waiters.items()):
                if waiter.error_code or waiter.response is not None:
                    continue
                outcome_unknown = False
                if ipc is not None:
                    try:
                        outcome_unknown = bool(ipc.record(command_id)["outcomeUnknown"])
                    except WorkerIpcFailure:
                        pass
                waiter.error_code = (
                    "WORKER_PROTOCOL_ERROR"
                    if code in {"WORKER_PROTOCOL_ERROR", "PARENT_EVENT_CAPACITY_FATAL"}
                    else "WORKER_DIED"
                )
                waiter.outcome_unknown = outcome_unknown
            epoch.readiness_event.set()
            epoch.state_ready_event.set()
            self._condition.notify_all()

    def _stderr_reader(self, epoch: _Epoch) -> None:
        try:
            raw = _read_bounded_line(epoch.handle.stderr)
            if raw is None:
                self._mark_failure(epoch, "WORKER_START_FAILED")
                return
            try:
                readiness = _parse_readiness(raw, epoch.handle.pid)
            except WorkerSupervisorError as error:
                self._mark_failure(epoch, _parse_startup_diagnostic(raw) if error.code == "WORKER_PROTOCOL_ERROR" else error.code)
                return
            with self._condition:
                epoch.readiness = readiness
                epoch.readiness_event.set()
                self._condition.notify_all()
            while not epoch.io_stop.is_set():
                raw = _read_bounded_line(epoch.handle.stderr)
                if raw is None:
                    if not epoch.stopping and not epoch.exit_event.is_set():
                        self._mark_failure(epoch, "WORKER_DIED")
                    return
                epoch.worker_diagnostic_code = (
                    _parse_worker_diagnostic_code(raw) or "INVALID_DIAGNOSTIC"
                )
                self._mark_failure(epoch, "WORKER_PROTOCOL_ERROR")
        except WorkerSupervisorError as error:
            if not epoch.stopping:
                self._mark_failure(epoch, error.code)

    def _stdout_reader(self, epoch: _Epoch) -> None:
        try:
            while not epoch.io_stop.is_set():
                raw = _read_bounded_line(epoch.handle.stdout)
                if raw is None:
                    if not epoch.stopping:
                        self._mark_failure(epoch, "WORKER_DIED")
                    return
                if not epoch.state_ready_event.is_set() or epoch.ipc is None:
                    self._mark_failure(epoch, "WORKER_PROTOCOL_ERROR")
                    return
                try:
                    delivery = epoch.ipc.accept_line(raw)
                except WorkerIpcFailure as error:
                    self._mark_failure(epoch, error.code)
                    return
                if delivery.kind == "quarantined":
                    continue
                if delivery.kind == "event":
                    event = delivery.value
                    if event is None:
                        self._mark_failure(epoch, "WORKER_PROTOCOL_ERROR")
                        return
                    with self._condition:
                        events_enabled = self._events_enabled
                    if not events_enabled:
                        epoch.worker_diagnostic_code = "EVENT_BEFORE_ADMISSION"
                        self._mark_failure(epoch, "WORKER_PROTOCOL_ERROR")
                        return
                    if event["event"] == "overflow":
                        self._mark_failure(epoch, "PARENT_EVENT_CAPACITY_FATAL")
                        return
                    if event["event"] == "fatal":
                        data = event.get("data")
                        epoch.worker_diagnostic_code = (
                            str(data.get("code"))
                            if isinstance(data, Mapping) and data.get("code")
                            else "WORKER_FATAL_EVENT"
                        )
                        self._mark_failure(epoch, "WORKER_PROTOCOL_ERROR")
                        return
                    with self._condition:
                        self._condition.notify_all()
                    continue
                if delivery.kind != "response" or delivery.command_id is None:
                    self._mark_failure(epoch, "WORKER_PROTOCOL_ERROR")
                    return
                with self._condition:
                    waiter = epoch.waiters.get(delivery.command_id)
                    if delivery.deliver_to_waiter:
                        if waiter is None or waiter.response is not None or waiter.error_code:
                            self._mark_failure(epoch, "WORKER_PROTOCOL_ERROR")
                            return
                        assert delivery.value is not None
                        waiter.response = delivery.value
                        waiter.outcome_unknown = delivery.outcome_unknown
                    self._condition.notify_all()
        except WorkerSupervisorError as error:
            if not epoch.stopping:
                self._mark_failure(epoch, error.code)

    def _stdin_writer(self, epoch: _Epoch) -> None:
        assert epoch.ipc is not None
        writer = ParentStdinWriter(epoch.ipc)
        try:
            descriptor = epoch.handle.stdin.fileno()
            while not epoch.io_stop.is_set():
                wrote = writer.write_one(descriptor, timeout=0.05)
                if not wrote and not epoch.ipc.health()["alive"]:
                    return
        except (OSError, ValueError, WorkerIpcFailure):
            if not epoch.stopping:
                self._mark_failure(epoch, "WORKER_DIED")

    def _process_waiter(self, epoch: _Epoch) -> None:
        try:
            exit_code = epoch.handle.wait(timeout=None)
            if type(exit_code) is not int:
                raise ValueError("invalid exit code")
        except BaseException:
            exit_code = -1
        with self._condition:
            epoch.exit_code = exit_code
            self._last_exit_code = exit_code
            epoch.exit_event.set()
            unexpected = not epoch.stopping
            self._condition.notify_all()
        if unexpected:
            logger.warning(
                "[DSOFTBUS_WORKER] exited pid=%s exitCode=%s "
                "failureCode=%s workerDiagnosticCode=%s",
                epoch.handle.pid,
                exit_code,
                epoch.failure_code or "none",
                epoch.worker_diagnostic_code or "none",
            )
            self._mark_failure(epoch, "WORKER_DIED")

    def _new_thread(
        self, *, name: str, target: Callable[[_Epoch], None], epoch: _Epoch
    ) -> threading.Thread:
        thread = self._thread_factory(
            target=target,
            args=(epoch,),
            name=name,
            daemon=False,
        )
        if not isinstance(thread, threading.Thread):
            _fail("WORKER_THREAD_START_FAILED")
        return thread

    def _start_epoch_threads(self, epoch: _Epoch) -> None:
        specifications = (
            ("waiter", "mclaw-dsoftbus-process-waiter", self._process_waiter),
            ("stderr", "mclaw-dsoftbus-stderr-reader", self._stderr_reader),
            ("stdout", "mclaw-dsoftbus-stdout-reader", self._stdout_reader),
        )
        try:
            for key, name, target in specifications:
                thread = self._new_thread(name=name, target=target, epoch=epoch)
                epoch.threads[key] = thread
                thread.start()
        except BaseException as error:
            self._mark_failure(epoch, "WORKER_THREAD_START_FAILED")
            self._force_process_exit(epoch)
            self._close_epoch_streams(epoch)
            self._join_epoch_threads(epoch)
            raise WorkerSupervisorError("WORKER_THREAD_START_FAILED") from error

    def _wait_condition(self, predicate: Callable[[], bool], absolute: float) -> bool:
        with self._condition:
            while not predicate():
                remaining = absolute - self._monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(min(0.05, remaining))
            return True

    def _request(
        self,
        epoch: _Epoch,
        operation: str,
        args: Mapping[str, Any],
        *,
        absolute_deadline: float,
        control: bool = False,
    ) -> Mapping[str, Any]:
        self._require_owner()
        ipc = epoch.ipc
        if ipc is None:
            _fail("WORKER_START_FAILED")
        command_id = str(uuid.uuid4())
        waiter = _ResponseWaiter()
        with self._condition:
            epoch.waiters[command_id] = waiter
        try:
            if control:
                ipc.admit_stop(command_id=command_id)
            else:
                ipc.admit(operation, args, command_id=command_id)
            self._operation_counts["stop" if control else operation] += 1
        except WorkerIpcFailure as error:
            with self._condition:
                epoch.waiters.pop(command_id, None)
            raise WorkerSupervisorError(error.code, phase=operation) from error

        completed = self._wait_condition(
            lambda: (
                waiter.response is not None
                or bool(waiter.error_code)
                or bool(epoch.failure_code)
            ),
            absolute_deadline,
        )
        if not completed:
            try:
                classification = ipc.timeout(command_id)
            except WorkerIpcFailure:
                classification = "outcomeUnknown"
            with self._condition:
                epoch.waiters.pop(command_id, None)
            _fail(
                "WORKER_CONTROL_TIMEOUT",
                outcome_unknown=classification == "outcomeUnknown",
                phase=operation,
            )
        with self._condition:
            epoch.waiters.pop(command_id, None)
        if waiter.error_code:
            _fail(
                waiter.error_code,
                outcome_unknown=waiter.outcome_unknown,
                phase=operation,
            )
        response = waiter.response
        if response is None:
            _fail(epoch.failure_code or "WORKER_DIED", phase=operation)
        if response["ok"] is not True:
            error = response["error"]
            native_code = int(error["nativeCode"])
            _fail(
                str(error["code"]),
                native_code=native_code if native_code != 0 else None,
                phase=operation,
            )
        return response["result"]

    def _verify_hello(
        self, epoch: _Epoch, result: Mapping[str, Any]
    ) -> VerifiedWorkerIdentity:
        assert epoch.readiness is not None
        identity = result["identity"]
        expected = self._identity
        if (
            result["workerEpoch"] != epoch.readiness.worker_epoch
            or result["socketCap"] != expected.socket_cap
            or identity["uid"] != expected.uid
            or identity["gid"] != expected.gid
            or tuple(identity["supplementaryGids"]) != expected.supplementary_gids
            or tuple(identity["capabilitySet"]) != expected.capability_set
            or identity["tokenIdHash"] != expected.token_id_hash
            or identity["selinuxDomain"] != expected.selinux_domain
            or identity["distributedDataSyncGranted"] is not True
        ):
            _fail("WORKER_IDENTITY_MISMATCH")
        local_udid = result["localUdid"]
        public_device_id = derive_public_device_id(local_udid)
        frozen_device_id = (
            self._verified.public_device_id
            if self._verified is not None
            else expected.expected_device_id
        )
        if frozen_device_id is not None and public_device_id != frozen_device_id:
            _fail("WORKER_IDENTITY_MISMATCH")
        digest = public_device_id.rsplit(":", 1)[-1]
        return VerifiedWorkerIdentity(
            public_device_id=public_device_id,
            public_agent_id=f"urn:mclaw:agent:{digest}",
            worker_epoch=epoch.readiness.worker_epoch,
            pid=epoch.handle.pid,
            start_time_ticks=epoch.handle.start_time_ticks,
            socket_cap=expected.socket_cap,
            local_udid=local_udid,
        )

    def _launch_epoch(self) -> VerifiedWorkerIdentity:
        if self._closed:
            _fail("RUNTIME_STOPPING")
        with self._condition:
            self._events_enabled = False
            self._native_started_epoch = None
        try:
            handle = self._launcher.spawn()
        except WorkerSupervisorError:
            raise
        except BaseException as error:
            raise WorkerSupervisorError("WORKER_LAUNCH_FAILED") from error
        if (
            type(handle.pid) is not int
            or not 1 <= handle.pid <= 2**31 - 1
            or type(handle.start_time_ticks) is not int
            or not 1 <= handle.start_time_ticks <= 2**63 - 1
            or handle.pid == os.getpid()
        ):
            try:
                handle.kill()
                handle.wait(timeout=_THREAD_JOIN_GRACE_S)
            except BaseException:
                pass
            _fail("WORKER_PROCESS_IDENTITY_INVALID")
        epoch = _Epoch(handle=handle)
        with self._condition:
            self._current = epoch
        try:
            self._start_epoch_threads(epoch)
            ready_deadline = self._monotonic() + self._control_timeout
            if not self._wait_condition(
                lambda: epoch.readiness_event.is_set() or bool(epoch.failure_code),
                ready_deadline,
            ):
                _fail("WORKER_START_TIMEOUT")
            if epoch.failure_code:
                _fail(epoch.failure_code)
            if epoch.readiness is None:
                _fail("WORKER_START_FAILED")
            epoch.ipc = WorkerIpcState(epoch.readiness.worker_epoch)
            epoch.state_ready_event.set()
            writer = self._new_thread(
                name="mclaw-dsoftbus-stdin-writer",
                target=self._stdin_writer,
                epoch=epoch,
            )
            epoch.threads["writer"] = writer
            writer.start()
            hello = self._request(
                epoch,
                "hello",
                {},
                absolute_deadline=self._monotonic() + self._control_timeout,
            )
            verified = self._verify_hello(epoch, hello)
            self._verified = verified
            self._last_pid = verified.pid
            self._last_start_ticks = verified.start_time_ticks
            self._last_epoch_digest = worker_epoch_digest(verified.worker_epoch)
            return verified
        except BaseException:
            self._cleanup_failed_epoch(epoch)
            raise

    def start(self) -> VerifiedWorkerIdentity:
        self._require_owner(establish=True)
        if self._first_start_attempted:
            _fail("WORKER_START_ALREADY_ATTEMPTED")
        self._first_start_attempted = True
        return self._launch_epoch()

    def complete_manifest_phase_b(self, gate: Mapping[str, Any]) -> None:
        """Freeze the identity-bound publication gate before Native ``start``."""

        self._require_owner()
        if not isinstance(gate, Mapping) or frozenset(gate) != frozenset(
            {"agentId", "deviceId", "manifest", "runtimeInstanceId"}
        ):
            _fail("PUBLICATION_GATE_INVALID")
        verified = self._verified
        epoch = self._current
        if (
            verified is None
            or epoch is None
            or epoch.failure_code
            or epoch.exit_event.is_set()
        ):
            _fail("WORKER_NOT_READY")
        try:
            runtime_instance_id = protocol.canonical_uuid4(
                gate["runtimeInstanceId"], "runtimeInstanceId"
            )
            from .manifest import validate_manifest_descriptor

            descriptor = validate_manifest_descriptor(dict(gate["manifest"]))
        except Exception as error:
            raise WorkerSupervisorError("PUBLICATION_GATE_INVALID") from error
        if (
            gate["deviceId"] != verified.public_device_id
            or gate["agentId"] != verified.public_agent_id
        ):
            _fail("WORKER_IDENTITY_MISMATCH")
        candidate = MappingProxyType(
            {
                "agentId": verified.public_agent_id,
                "deviceId": verified.public_device_id,
                "manifest": descriptor.as_mapping(),
                "runtimeInstanceId": runtime_instance_id,
            }
        )
        if self._publication_gate is not None:
            if dict(self._publication_gate) != dict(candidate):
                _fail("PUBLICATION_GATE_CONFLICT")
            return
        self._publication_gate = candidate

    def _phase_b_epoch(self, *, native_started: bool = True) -> _Epoch:
        self._require_owner()
        epoch = self._current
        if (
            self._publication_gate is None
            or self._verified is None
            or epoch is None
            or epoch.failure_code
            or epoch.exit_event.is_set()
            or epoch.ipc is None
        ):
            _fail("WORKER_NOT_READY")
        if native_started and self._native_started_epoch != self._verified.worker_epoch:
            _fail("WORKER_NOT_READY")
        return epoch

    def _business_request(
        self,
        operation: str,
        args: Mapping[str, Any],
        *,
        timeout_s: float | None = None,
    ) -> Mapping[str, Any]:
        epoch = self._phase_b_epoch()
        return self._request(
            epoch,
            operation,
            args,
            absolute_deadline=(
                self._monotonic()
                + (self._control_timeout if timeout_s is None else timeout_s)
            ),
        )

    def _device_manager_request(
        self, operation: str, args: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return self._business_request(
            operation,
            args,
            timeout_s=max(
                self._control_timeout,
                float(protocol.DEVICE_MANAGER_WORKER_TIMEOUT_S),
            ),
        )

    def start_node_events(self) -> None:
        """Register the Native node callback only after the publication gate."""

        epoch = self._phase_b_epoch(native_started=False)
        assert self._verified is not None
        if self._native_started_epoch is not None:
            _fail("WORKER_START_ALREADY_ATTEMPTED")
        with self._condition:
            self._events_enabled = True
        try:
            self._request(
                epoch,
                "start",
                {},
                absolute_deadline=self._monotonic() + self._control_timeout,
            )
        except BaseException:
            with self._condition:
                self._events_enabled = False
            raise
        self._native_started_epoch = self._verified.worker_epoch

    def snapshot_nodes_page(
        self, *, snapshot_id: str = "", cursor: str = ""
    ) -> Mapping[str, Any]:
        if (snapshot_id == "") != (cursor == ""):
            _fail("SNAPSHOT_CURSOR_INVALID")
        args: Mapping[str, Any] = (
            MappingProxyType({})
            if not snapshot_id
            else MappingProxyType({"cursor": cursor, "snapshotId": snapshot_id})
        )
        return self._business_request("snapshot_nodes", args)

    def get_node_udid(self, network_id: str) -> str:
        result = self._business_request(
            "get_node_udid", MappingProxyType({"networkId": network_id})
        )
        return str(result["udid"])

    def start_device_discovery(self) -> None:
        """Start one M-Claw-owned DeviceManager discovery session."""

        result = self._device_manager_request(
            "start_device_discovery", MappingProxyType({})
        )
        if result["started"] is not True:
            _fail("WORKER_PROTOCOL_ERROR")

    def stop_device_discovery(self) -> Mapping[str, Any]:
        """Stop discovery and return only redacted candidate identifiers."""

        result = self._device_manager_request(
            "stop_device_discovery", MappingProxyType({})
        )
        return MappingProxyType(
            {
                "devices": tuple(
                    MappingProxyType(
                        {
                            "deviceIdSha256": str(device["deviceIdSha256"]),
                            "deviceName": str(device["deviceName"]),
                            "deviceTypeId": int(device["deviceTypeId"]),
                            "networkIdSha256": str(device["networkIdSha256"]),
                            "publicDeviceId": str(device["publicDeviceId"]),
                        }
                    )
                    for device in result["devices"]
                ),
                "failureNativeCode": result["failureNativeCode"],
                "stopped": bool(result["stopped"]),
            }
        )

    def begin_device_bind(self, device_id_sha256: str) -> Mapping[str, Any]:
        """Begin one system-confirmed app-level DeviceManager bind."""

        result = self._device_manager_request(
            "begin_device_bind",
            MappingProxyType({"deviceIdSha256": device_id_sha256}),
        )
        return MappingProxyType(
            {
                "binding": bool(result["binding"]),
                "deviceIdSha256": str(result["deviceIdSha256"]),
            }
        )

    def get_device_bind_status(
        self, device_id_sha256: str
    ) -> Mapping[str, Any]:
        """Read the terminal or pending outcome of one local bind request."""

        result = self._device_manager_request(
            "get_device_bind_status",
            MappingProxyType({"deviceIdSha256": device_id_sha256}),
        )
        return MappingProxyType(
            {
                "deviceIdSha256": str(result["deviceIdSha256"]),
                "nativeCode": int(result["nativeCode"]),
                "status": str(result["status"]),
            }
        )

    def list_trusted_devices(self) -> tuple[Mapping[str, Any], ...]:
        result = self._device_manager_request(
            "list_trusted_devices", MappingProxyType({})
        )
        return tuple(
            MappingProxyType(
                {
                    "deviceIdSha256": str(device["deviceIdSha256"]),
                    "deviceName": str(device["deviceName"]),
                    "deviceTypeId": int(device["deviceTypeId"]),
                    "networkId": str(device["networkId"]),
                }
            )
            for device in result["devices"]
        )

    def unbind_device(self, network_id: str) -> Mapping[str, Any]:
        result = self._device_manager_request(
            "unbind_device", MappingProxyType({"networkId": network_id})
        )
        return MappingProxyType(
            {
                "deviceIdSha256": str(result["deviceIdSha256"]),
                "unbound": bool(result["unbound"]),
            }
        )

    def listen(self) -> int:
        result = self._business_request(
            "listen", MappingProxyType({"serviceName": protocol.SERVICE_NAME})
        )
        return int(result["socket"])

    def connect(self, network_id: str) -> Mapping[str, int]:
        result = self._business_request(
            "connect",
            MappingProxyType(
                {
                    "networkId": network_id,
                    "peerServiceName": protocol.SERVICE_NAME,
                    "serviceName": protocol.CLIENT_SERVICE_NAME,
                }
            ),
        )
        return MappingProxyType(
            {"mtu": int(result["mtu"]), "socket": int(result["socket"])}
        )

    def send_bytes(self, socket: int, data: bytes) -> int:
        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")
        result = self._business_request(
            "send_bytes",
            MappingProxyType(
                {
                    "data": base64.b64encode(data).decode("ascii"),
                    "socket": socket,
                }
            ),
        )
        return int(result["sentBytes"])

    def close_socket(self, socket: int) -> None:
        self._business_request(
            "close_socket", MappingProxyType({"socket": socket})
        )

    def pop_event(self) -> Mapping[str, Any] | None:
        epoch = self._phase_b_epoch()
        assert epoch.ipc is not None
        return epoch.ipc.pop_event()

    def wait_event(self, absolute_deadline: float) -> Mapping[str, Any] | None:
        """Wait on the bounded IPC event queue without creating a side queue."""

        epoch = self._phase_b_epoch()
        assert epoch.ipc is not None
        if not self._wait_condition(
            lambda: (
                epoch.ipc is not None
                and epoch.ipc.health()["parentEventCount"] > 0
            )
            or bool(epoch.failure_code)
            or epoch.exit_event.is_set(),
            absolute_deadline,
        ):
            return None
        if epoch.failure_code or epoch.exit_event.is_set():
            _fail(epoch.failure_code or "WORKER_DIED")
        return epoch.ipc.pop_event()

    def enable_recovery_after_ready(self) -> None:
        """Open restart admission only after the full Runtime has reached READY."""

        self._require_owner()
        epoch = self._current
        if epoch is None or self._verified is None or epoch.failure_code or epoch.exit_event.is_set():
            _fail("WORKER_NOT_READY")
        self._recovery_enabled = True

    def recover(self) -> VerifiedWorkerIdentity:
        """Perform one bounded restart attempt after an established epoch died."""

        self._require_owner()
        if not self._recovery_enabled:
            _fail("WORKER_RESTART_NOT_ALLOWED")
        epoch = self._current
        if epoch is None or (not epoch.failure_code and not epoch.exit_event.is_set()):
            _fail("WORKER_RESTART_NOT_REQUIRED")
        self._cleanup_failed_epoch(epoch)
        now = self._monotonic()
        while self._restart_times and self._restart_times[0] <= now - protocol.WORKER_RESTART_WINDOW_S:
            self._restart_times.popleft()
        if len(self._restart_times) >= protocol.WORKER_RESTART_LIMIT:
            self._restart_exhausted = True
            _fail("WORKER_RESTART_EXHAUSTED")
        self._restart_times.append(now)
        self._restart_count += 1
        return self._launch_epoch()

    def begin_shutdown(self) -> None:
        self._require_owner()
        epoch = self._current
        if epoch is None:
            self._closed = True
            return
        epoch.stopping = True
        if epoch.ipc is not None:
            epoch.ipc.close_business_admission()
            epoch.ipc.cancel_queued()

    def _force_process_exit(self, epoch: _Epoch, *, absolute_deadline: float | None = None) -> None:
        if epoch.exit_event.is_set():
            return
        try:
            epoch.handle.terminate()
        except BaseException:
            pass
        waiter = epoch.threads.get("waiter")
        if waiter is None or waiter.ident is None:
            try:
                epoch.exit_code = epoch.handle.wait(timeout=0.10)
                epoch.exit_event.set()
                return
            except BaseException:
                pass
        terminate_until = self._monotonic() + 0.10
        if absolute_deadline is not None:
            terminate_until = min(terminate_until, absolute_deadline)
        self._wait_condition(epoch.exit_event.is_set, terminate_until)
        if epoch.exit_event.is_set():
            return
        try:
            epoch.handle.kill()
        except BaseException:
            pass
        kill_until = self._monotonic() + _THREAD_JOIN_GRACE_S
        if absolute_deadline is not None and absolute_deadline > self._monotonic():
            kill_until = min(kill_until, absolute_deadline)
        self._wait_condition(epoch.exit_event.is_set, kill_until)
        if not epoch.exit_event.is_set():
            try:
                exit_code = epoch.handle.wait(timeout=0)
            except BaseException:
                _fail("WORKER_REAP_FAILED")
            epoch.exit_code = exit_code
            epoch.exit_event.set()

    @staticmethod
    def _close_epoch_streams(epoch: _Epoch) -> None:
        epoch.io_stop.set()
        if epoch.ipc is not None:
            epoch.ipc.fail_epoch()
        for stream in (epoch.handle.stdin, epoch.handle.stdout, epoch.handle.stderr):
            try:
                stream.close()
            except BaseException:
                pass

    def _join_epoch_threads(self, epoch: _Epoch) -> None:
        deadline = self._monotonic() + _THREAD_JOIN_GRACE_S
        for thread in tuple(epoch.threads.values()):
            if thread is threading.current_thread() or thread.ident is None:
                continue
            remaining = max(0.0, deadline - self._monotonic())
            thread.join(remaining)
        if any(thread.is_alive() for thread in epoch.threads.values()):
            _fail("WORKER_THREAD_JOIN_FAILED")

    def _cleanup_failed_epoch(self, epoch: _Epoch) -> None:
        epoch.stopping = True
        self._mark_failure(epoch, epoch.failure_code or "WORKER_DIED")
        self._force_process_exit(epoch)
        self._close_epoch_streams(epoch)
        self._join_epoch_threads(epoch)
        with self._condition:
            epoch.waiters.clear()

    def stop(self, absolute_deadline: float) -> None:
        self._require_owner()
        if isinstance(absolute_deadline, bool) or not isinstance(absolute_deadline, (int, float)):
            raise TypeError("absolute_deadline must be a finite number")
        deadline = float(absolute_deadline)
        if deadline != deadline or deadline in {float("inf"), float("-inf")}:
            raise ValueError("absolute_deadline must be a finite number")
        if self._closed and self._current is None:
            return
        self._closed = True
        epoch = self._current
        if epoch is None:
            return
        self.begin_shutdown()
        cooperative = False
        if epoch.ipc is not None and epoch.ipc.health()["alive"] and not epoch.exit_event.is_set():
            try:
                result = self._request(
                    epoch,
                    "stop",
                    {},
                    absolute_deadline=min(
                        deadline, self._monotonic() + self._control_timeout
                    ),
                    control=True,
                )
                cooperative = dict(result) == {"stopped": True}
            except WorkerSupervisorError:
                cooperative = False
        if cooperative:
            self._wait_condition(epoch.exit_event.is_set, deadline)
        if not epoch.exit_event.is_set():
            self._force_process_exit(epoch, absolute_deadline=deadline)
        self._close_epoch_streams(epoch)
        self._join_epoch_threads(epoch)
        with self._condition:
            epoch.waiters.clear()
        self._current = None
        self._recovery_enabled = False
        self._native_started_epoch = None

    def emergency_reap(self) -> None:
        """Best-effort host-thread fallback; it never publishes health."""

        with self._condition:
            epoch = self._current
            self._closed = True
            self._events_enabled = False
        if epoch is None:
            return
        epoch.stopping = True
        try:
            self._force_process_exit(epoch)
        except BaseException:
            pass
        self._close_epoch_streams(epoch)
        try:
            self._join_epoch_threads(epoch)
        except BaseException:
            pass
        with self._condition:
            epoch.waiters.clear()
            if self._current is epoch:
                self._current = None

    def health_updates(self) -> Mapping[str, Any]:
        self._require_owner()
        epoch = self._current
        ipc_health: Mapping[str, Any] = MappingProxyType(
            {
                "parentCommandQueueBytes": 0,
                "parentCommandQueueCount": 0,
                "parentEventBytes": 0,
                "parentEventCount": 0,
                "parentResponseRouteBytes": 0,
                "parentResponseRouteCount": 0,
            }
        )
        alive = False
        if epoch is not None and epoch.ipc is not None:
            ipc_health = epoch.ipc.health()
            alive = (
                self._verified is not None
                and not epoch.failure_code
                and not epoch.exit_event.is_set()
                and bool(ipc_health["alive"])
            )
        return MappingProxyType(
            {
                "eventOverflowCount": self._event_overflow_count,
                "parentCommandQueueBytes": ipc_health["parentCommandQueueBytes"],
                "parentCommandQueueCount": ipc_health["parentCommandQueueCount"],
                "parentEventBytes": ipc_health["parentEventBytes"],
                "parentEventDepth": ipc_health.get("parentEventCount", 0),
                "parentResponseRouteBytes": ipc_health["parentResponseRouteBytes"],
                "parentResponseRouteCount": ipc_health["parentResponseRouteCount"],
                "restartCount": self._restart_count,
                "workerAlive": alive,
                "workerEpoch": self._last_epoch_digest,
                "workerEventBytes": 0,
                "workerEventDepth": 0,
                "workerPid": self._last_pid,
                "workerStartTimeTicks": self._last_start_ticks,
            }
        )

    def diagnostic_snapshot(self) -> Mapping[str, Any]:
        """Return only bounded, non-sensitive lifecycle facts."""

        self._require_owner()
        epoch = self._current
        return MappingProxyType(
            {
                "activeThreadCount": (
                    sum(1 for thread in epoch.threads.values() if thread.is_alive())
                    if epoch is not None
                    else 0
                ),
                "closed": self._closed,
                "firstStartAttempted": self._first_start_attempted,
                "manifestPhaseBComplete": self._publication_gate is not None,
                "lastExitCode": self._last_exit_code,
                "lastFailureCode": self._last_failure_code,
                "lastWorkerDiagnosticCode": self._last_worker_diagnostic_code,
                "nativeNodeEventsStarted": (
                    self._verified is not None
                    and self._native_started_epoch == self._verified.worker_epoch
                ),
                "restartCount": self._restart_count,
                "restartExhausted": self._restart_exhausted,
                "operationCounts": MappingProxyType(dict(self._operation_counts)),
                "workerAlive": bool(self.health_updates()["workerAlive"]),
            }
        )


__all__ = [
    "ProfileWorkerLauncher",
    "SubprocessWorkerLauncher",
    "VerifiedWorkerIdentity",
    "WorkerIdentityExpectation",
    "WorkerLauncher",
    "WorkerProcessHandle",
    "WorkerSupervisor",
    "WorkerSupervisorError",
]
