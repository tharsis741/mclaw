# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import base64
import hashlib
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import pytest

from mclaw.dsoftbus import protocol
from mclaw.dsoftbus import task_source as task_source_module
from mclaw.dsoftbus.a2a import A2AError, validate_core_method
from mclaw.dsoftbus.a2a_media import (
    TASK_INPUT_REFERENCE_PREFIX,
    task_input_manifest_from_parts,
    task_input_reference_part,
)
from mclaw.dsoftbus.agent_message import RemoteTurnRequest
from mclaw.dsoftbus.agent_task import DsoftbusTaskDispatcher
from mclaw.dsoftbus.task_files import (
    InboundTaskFileStore,
    OutboundTaskFileStore,
    TaskFileError,
    TaskInputByteBudget,
    TaskInputDescriptor,
    safe_relative_path,
)
from mclaw.dsoftbus.task_source import (
    LocalTaskSourceService,
    RemoteTaskSourceClient,
)
from mclaw.dsoftbus import workspace as workspace_module
from mclaw.dsoftbus.workspace import DsoftbusWorkspace, RemoteWorkspaceError


PEER = "urn:mclaw:device:oh:" + "b" * 64
RUNTIME_ID = "22222222-2222-4222-8222-222222222222"
MESSAGE_ID = "12345678-1234-4234-9234-123456789abc"
TASK_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
INPUT_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _config() -> dict[str, Any]:
    return {
        "accept_remote_messages": True,
        "global_requests_per_minute": 10,
        "per_peer_requests_per_minute": 5,
        "remote_token_budget_per_hour": 100_000,
    }


class _Executor:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.requests: list[RemoteTurnRequest] = []

    async def execute(self, request: RemoteTurnRequest) -> Mapping[str, Any]:
        self.requests.append(request)
        self.started.set()
        await self.release.wait()
        return MappingProxyType(
            {
                "completed": True,
                "interrupted": False,
                "final_response": "done",
                "messages": [
                    {"role": "user", "content": request.text},
                    {"role": "assistant", "content": "done"},
                ],
                "token_usage": {"input_tokens": 1, "output_tokens": 1},
            }
        )

    @staticmethod
    def estimate_budget(_request: RemoteTurnRequest) -> int:
        return 100

    @staticmethod
    def update_provider_runtime(_context: Any | None) -> None:
        return None

    @staticmethod
    def interrupt(_session_id: str) -> bool:
        return True

    async def cancel_and_reap(
        self, _session_id: str, _task_id: str, _deadline: float
    ) -> bool:
        self.release.set()
        return True

    @staticmethod
    async def forget_session(_session_id: str, _deadline: float) -> bool:
        return True

    @staticmethod
    async def dispose(_deadline: float) -> bool:
        return True


def _descriptor(
    content: bytes,
    *,
    filename: str = "sample.bin",
    media_type: str = "application/octet-stream",
) -> TaskInputDescriptor:
    return TaskInputDescriptor(
        input_id=INPUT_ID,
        relative_path=f"attachments/{INPUT_ID}/{filename}",
        filename=filename,
        media_type=media_type,
        byte_length=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def test_snapshot_and_chunk_assembly_support_files_larger_than_old_frame_limit(
    tmp_path: Path,
) -> None:
    content = bytes(range(251)) * 200
    source = tmp_path / "sample.bin"
    source.write_bytes(content)
    caller = DsoftbusWorkspace(tmp_path / "caller")
    prepared = OutboundTaskFileStore(
        caller,
        uuid_factory=lambda: INPUT_ID,
    ).prepare(PEER, MESSAGE_ID, (str(source.resolve()),))
    prepared = prepared.bind_task(caller, TASK_ID)
    descriptor, snapshot = prepared.files[0]
    assert descriptor.byte_length > 16_384
    assert snapshot.read_bytes() == content

    receiver = DsoftbusWorkspace(tmp_path / "receiver")
    paths = receiver.ensure_task("executing", PEER, TASK_ID)
    incoming = InboundTaskFileStore(paths, (descriptor,))
    offset = incoming.begin(INPUT_ID)
    while offset < len(content):
        chunk = content[offset : offset + protocol.TASK_TRANSFER_CHUNK_BYTES_MAX]
        offset = incoming.append(INPUT_ID, offset, chunk)
    receipt = incoming.commit(INPUT_ID)

    assert receipt["byteLength"] == len(content)
    assert receipt["sha256"] == hashlib.sha256(content).hexdigest()
    assert incoming.ready is True
    assert paths.input is not None and paths.work is not None
    assert (paths.input / descriptor.relative_path).read_bytes() == content
    assert (paths.work / descriptor.relative_path).read_bytes() == content


def test_empty_input_file_is_a_valid_verified_task_copy(tmp_path: Path) -> None:
    source = tmp_path / "empty.txt"
    source.touch()
    caller = DsoftbusWorkspace(tmp_path / "caller-empty")
    prepared = OutboundTaskFileStore(
        caller,
        uuid_factory=lambda: INPUT_ID,
    ).prepare(PEER, MESSAGE_ID, (str(source.resolve()),))
    descriptor, _snapshot = prepared.files[0]
    assert descriptor.byte_length == 0
    assert descriptor.sha256 == hashlib.sha256(b"").hexdigest()

    receiver = DsoftbusWorkspace(tmp_path / "receiver-empty")
    paths = receiver.ensure_task("executing", PEER, TASK_ID)
    incoming = InboundTaskFileStore(paths, (descriptor,))
    assert incoming.begin(INPUT_ID) == 0
    receipt = incoming.commit(INPUT_ID)

    assert receipt["byteLength"] == 0
    assert paths.work is not None
    assert (paths.work / descriptor.relative_path).read_bytes() == b""


def test_hash_mismatch_never_publishes_a_working_copy(tmp_path: Path) -> None:
    content = b"declared content"
    descriptor = _descriptor(content)
    receiver = DsoftbusWorkspace(tmp_path / "receiver")
    paths = receiver.ensure_task("executing", PEER, TASK_ID)
    incoming = InboundTaskFileStore(paths, (descriptor,))
    incoming.begin(INPUT_ID)
    incoming.append(INPUT_ID, 0, b"altered content!")

    with pytest.raises(TaskFileError, match="TASK_INPUT_HASH_MISMATCH"):
        incoming.commit(INPUT_ID)
    assert paths.work is not None
    assert not (paths.work / descriptor.relative_path).exists()


def test_work_copy_does_not_follow_agent_created_parent_symlink(
    tmp_path: Path,
) -> None:
    content = b"trusted source bytes"
    descriptor = _descriptor(content)
    receiver = DsoftbusWorkspace(tmp_path / "receiver-symlink")
    paths = receiver.ensure_task("executing", PEER, TASK_ID)
    assert paths.work is not None
    outside = tmp_path / "outside-workspace"
    outside.mkdir()
    try:
        (paths.work / "attachments").symlink_to(
            outside,
            target_is_directory=True,
        )
    except OSError as error:
        pytest.skip(f"symbolic links are unavailable: {error}")
    incoming = InboundTaskFileStore(paths, (descriptor,))
    incoming.begin(INPUT_ID)
    incoming.append(INPUT_ID, 0, content)

    with pytest.raises(TaskFileError, match="SOURCE_PATH_FORBIDDEN"):
        incoming.commit(INPUT_ID)

    assert list(outside.iterdir()) == []


def test_posix_not_a_directory_is_normalized_as_workspace_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workspace_module.os, "O_DIRECTORY", 0, raising=False)
    monkeypatch.setattr(workspace_module.os, "O_NOFOLLOW", 0, raising=False)
    monkeypatch.setattr(
        workspace_module.os,
        "mkdir",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileExistsError()),
    )
    monkeypatch.setattr(
        workspace_module.os,
        "open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(NotADirectoryError()),
    )

    with pytest.raises(
        RemoteWorkspaceError,
        match="Remote workspace child is not a private directory",
    ):
        workspace_module._open_or_create_private_child(7, "attachments")


@pytest.mark.parametrize("value", ("../secret", "/absolute", "a\\b", "a/../b"))
def test_relative_paths_reject_escape_forms(value: str) -> None:
    with pytest.raises(TaskFileError, match="SOURCE_PATH_FORBIDDEN"):
        safe_relative_path(value)


def _source_service(
    tmp_path: Path, source: Path
) -> tuple[LocalTaskSourceService, str]:
    workspace = DsoftbusWorkspace(tmp_path / "source-workspace")
    prepared = OutboundTaskFileStore(
        workspace,
        uuid_factory=lambda: INPUT_ID,
    ).prepare(PEER, MESSAGE_ID, (str(source.resolve()),))
    prepared = prepared.bind_task(workspace, TASK_ID)
    return LocalTaskSourceService(prepared), prepared.scopes[0].scope_id


def test_directory_scope_never_lists_or_follows_symbolic_links(
    tmp_path: Path,
) -> None:
    source = tmp_path / "project"
    source.mkdir()
    (source / "visible.txt").write_text("visible", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    try:
        (source / "escape").symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symbolic links are unavailable: {error}")

    service, scope_id = _source_service(tmp_path, source)
    listing = service.list_entries(
        scope_id=scope_id,
        relative_path="",
        depth=3,
        page_size=protocol.TASK_SOURCE_PAGE_MAX,
        page_token="",
    )

    assert [entry["path"] for entry in listing["entries"]] == ["visible.txt"]
    with pytest.raises(TaskFileError, match="SOURCE_PATH_FORBIDDEN"):
        service.open_snapshot(
            scope_id=scope_id,
            relative_path="escape/secret.txt",
            transfer_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        )


def test_directory_scope_detects_replaced_root_before_serving(
    tmp_path: Path,
) -> None:
    source = tmp_path / "project"
    source.mkdir()
    (source / "original.txt").write_text("original", encoding="utf-8")
    service, scope_id = _source_service(tmp_path, source)
    original = tmp_path / "project-original"
    source.rename(original)
    source.mkdir()
    (source / "replacement.txt").write_text("replacement", encoding="utf-8")

    with pytest.raises(TaskFileError, match="SOURCE_CHANGED"):
        service.list_entries(
            scope_id=scope_id,
            relative_path="",
            depth=1,
            page_size=protocol.TASK_SOURCE_PAGE_MAX,
            page_token="",
        )


def test_directory_scope_pagination_uses_global_path_order(tmp_path: Path) -> None:
    source = tmp_path / "project"
    (source / "a").mkdir(parents=True)
    (source / "a" / "z").write_text("nested", encoding="utf-8")
    (source / "a.txt").write_text("sibling", encoding="utf-8")
    service, scope_id = _source_service(tmp_path, source)

    first = service.list_entries(
        scope_id=scope_id,
        relative_path="",
        depth=2,
        page_size=2,
        page_token="",
    )
    second = service.list_entries(
        scope_id=scope_id,
        relative_path="",
        depth=2,
        page_size=2,
        page_token=first["nextPageToken"],
    )

    assert [row["path"] for row in first["entries"]] == ["a", "a.txt"]
    assert first["nextPageToken"] == "a.txt"
    assert [row["path"] for row in second["entries"]] == ["a/z"]
    assert second["nextPageToken"] == ""


def test_directory_scan_cap_applies_before_sorting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "project"
    source.mkdir()
    for index in range(5):
        (source / f"item-{index}.txt").write_text("x", encoding="utf-8")
    service, scope_id = _source_service(tmp_path, source)
    real_scandir = task_source_module.os.scandir
    calls = 0

    class CountingScandir:
        def __init__(self, path: Any) -> None:
            self._inner = real_scandir(path)
            self._iterator: Any = None

        def __enter__(self) -> "CountingScandir":
            self._iterator = self._inner.__enter__()
            return self

        def __exit__(self, *args: Any) -> Any:
            return self._inner.__exit__(*args)

        def __iter__(self) -> "CountingScandir":
            return self

        def __next__(self) -> Any:
            nonlocal calls
            calls += 1
            return next(self._iterator)

    monkeypatch.setattr(protocol, "TASK_SOURCE_SCAN_ENTRY_MAX", 3)
    monkeypatch.setattr(task_source_module.os, "scandir", CountingScandir)
    result = service.list_entries(
        scope_id=scope_id,
        relative_path="",
        depth=1,
        page_size=1,
        page_token="",
    )

    assert calls == 3
    assert result["contextLimited"] is True


def test_content_search_reads_past_the_first_mebibyte(tmp_path: Path) -> None:
    source = tmp_path / "project"
    source.mkdir()
    target = source / "large.txt"
    target.write_bytes((b"ordinary line\n" * 90_000) + b"late unique marker\n")
    assert target.stat().st_size > 1_048_576
    service, scope_id = _source_service(tmp_path, source)

    result = service.search(
        scope_id=scope_id,
        relative_path="",
        query="late unique marker",
        mode="content",
        max_results=10,
    )

    assert [match["path"] for match in result["matches"]] == ["large.txt"]
    assert result["scanLimited"] is False


def test_content_search_marks_global_byte_truncation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "project"
    source.mkdir()
    (source / "large.txt").write_bytes(b"ordinary content\n" * 20)
    service, scope_id = _source_service(tmp_path, source)
    monkeypatch.setattr(protocol, "TASK_SOURCE_SEARCH_BYTES_MAX", 64)

    result = service.search(
        scope_id=scope_id,
        relative_path="",
        query="not present",
        mode="content",
        max_results=10,
    )

    assert result["matches"] == ()
    assert result["scanLimited"] is True


def test_source_fetch_uses_the_shared_task_byte_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(protocol, "TASK_INPUT_TASK_BYTES_MAX", 10)
        workspace = DsoftbusWorkspace(tmp_path / "workspace")
        paths = workspace.ensure_task("executing", PEER, TASK_ID)
        budget = TaskInputByteBudget(6)
        requested: list[str] = []

        async def requester(
            method: str, params: Mapping[str, Any]
        ) -> Mapping[str, Any]:
            requested.append(method)
            assert method == "mclaw.taskSource.open"
            descriptor = TaskInputDescriptor(
                input_id=str(params["transferId"]),
                relative_path="source/file.bin",
                filename="file.bin",
                media_type="application/octet-stream",
                byte_length=5,
                sha256=hashlib.sha256(b"12345").hexdigest(),
            )
            return descriptor.wire_value()

        client = RemoteTaskSourceClient(
            owner_loop=asyncio.get_running_loop(),
            requester=requester,
            task_id=TASK_ID,
            workspace=paths,
            source_scopes=(),
            input_byte_budget=budget,
        )
        with pytest.raises(TaskFileError, match="SOURCE_QUOTA_EXCEEDED"):
            await client.fetch(scope_id=INPUT_ID, paths=("file.bin",))
        assert requested == ["mclaw.taskSource.open"]
        assert budget.used_bytes == 6

    asyncio.run(scenario())


def test_source_fetch_retry_reuses_transfer_and_completed_batch_files(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        contents = {
            "one.bin": b"first",
            "two.bin": b"second",
        }
        workspace = DsoftbusWorkspace(tmp_path / "workspace-retry")
        paths = workspace.ensure_task("executing", PEER, TASK_ID)
        budget = TaskInputByteBudget()
        opened: dict[str, list[str]] = {name: [] for name in contents}
        transfer_paths: dict[str, str] = {}
        request_count = 0
        interrupt_second = True

        async def requester(
            method: str, params: Mapping[str, Any]
        ) -> Mapping[str, Any]:
            nonlocal interrupt_second, request_count
            request_count += 1
            transfer_id = str(params["transferId"])
            if method == "mclaw.taskSource.open":
                relative = str(params["path"])
                opened[relative].append(transfer_id)
                transfer_paths[transfer_id] = relative
                content = contents[relative]
                return TaskInputDescriptor(
                    input_id=transfer_id,
                    relative_path=f"sources/{INPUT_ID}/{relative}",
                    filename=relative,
                    media_type="application/octet-stream",
                    byte_length=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                ).wire_value()
            assert method == "mclaw.taskSource.read"
            relative = transfer_paths[transfer_id]
            if relative == "two.bin" and interrupt_second:
                interrupt_second = False
                raise TaskFileError("DEADLINE_EXCEEDED")
            content = contents[relative]
            offset = int(params["offset"])
            raw = content[offset : offset + protocol.TASK_TRANSFER_CHUNK_BYTES_MAX]
            return {
                "transferId": transfer_id,
                "offset": offset,
                "nextOffset": offset + len(raw),
                "data": base64.b64encode(raw).decode("ascii"),
                "eof": offset + len(raw) == len(content),
            }

        client = RemoteTaskSourceClient(
            owner_loop=asyncio.get_running_loop(),
            requester=requester,
            task_id=TASK_ID,
            workspace=paths,
            source_scopes=(),
            input_byte_budget=budget,
        )

        with pytest.raises(TaskFileError, match="DEADLINE_EXCEEDED"):
            await client.fetch(
                scope_id=INPUT_ID,
                paths=("one.bin", "two.bin"),
            )
        assert budget.used_bytes == len(contents["one.bin"])

        completed = await client.fetch(
            scope_id=INPUT_ID,
            paths=("one.bin", "two.bin"),
        )
        assert budget.used_bytes == sum(map(len, contents.values()))
        assert len(opened["one.bin"]) == 1
        assert len(opened["two.bin"]) == 2
        assert opened["two.bin"][0] == opened["two.bin"][1]
        assert [Path(item["localPath"]).read_bytes() for item in completed["files"]] == [
            contents["one.bin"],
            contents["two.bin"],
        ]

        completed_request_count = request_count
        repeated = await client.fetch(
            scope_id=INPUT_ID,
            paths=("one.bin", "two.bin"),
        )
        assert request_count == completed_request_count
        assert repeated == completed

    asyncio.run(scenario())


def test_outbound_snapshot_enforces_task_bytes_before_copying_later_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(protocol, "TASK_INPUT_TASK_BYTES_MAX", 10)
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(b"123456")
    second.write_bytes(b"12345")
    root = tmp_path / "workspace-total"
    store = OutboundTaskFileStore(DsoftbusWorkspace(root))

    with pytest.raises(TaskFileError, match="TASK_INPUT_TOO_LARGE"):
        store.prepare(
            PEER,
            MESSAGE_ID,
            (str(first.resolve()), str(second.resolve())),
        )

    assert list(root.rglob("*.snapshot")) == []


def test_input_path_limit_reserves_one_a2a_text_part(tmp_path: Path) -> None:
    assert protocol.TASK_INPUT_PATH_MAX == 31
    store = OutboundTaskFileStore(DsoftbusWorkspace(tmp_path / "workspace"))
    paths = tuple(str((tmp_path / f"missing-{index}").resolve()) for index in range(32))
    with pytest.raises(TaskFileError, match="TASK_INPUT_INVALID"):
        store.prepare(PEER, MESSAGE_ID, paths)


def test_dispatch_waits_for_verified_input_before_starting_agent(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        content = bytes(range(199)) * 150
        descriptor = _descriptor(
            content,
            filename="sample.png",
            media_type="image/png",
        )
        call = validate_core_method(
            "SendStreamingMessage",
            {
                "message": {
                    "messageId": MESSAGE_ID,
                    "role": "ROLE_USER",
                    "extensions": [protocol.TASK_FILES_EXTENSION_URI],
                    "parts": [
                        {"text": "分析输入文件"},
                        dict(task_input_reference_part(descriptor)),
                    ],
                }
            },
        )
        executor = _Executor()
        workspace = DsoftbusWorkspace(tmp_path / "workspace")
        dispatcher = DsoftbusTaskDispatcher(
            config=_config(),
            executor=executor,
            provider_runtime=object(),
            provider_ready=True,
            state_root=tmp_path / "state",
            workspace=workspace,
        )
        task, subscription = await dispatcher.submit(
            peer_device_id=PEER,
            peer_runtime_instance_id=RUNTIME_ID,
            call=call,
            subscribe=True,
        )
        assert subscription is not None
        await asyncio.sleep(0)
        assert not executor.started.is_set()

        begin = await dispatcher.begin_task_input(
            PEER, RUNTIME_ID, task["id"], INPUT_ID
        )
        offset = begin["nextOffset"]
        while offset < len(content):
            chunk = content[offset : offset + protocol.TASK_TRANSFER_CHUNK_BYTES_MAX]
            appended = await dispatcher.append_task_input(
                PEER, RUNTIME_ID, task["id"], INPUT_ID, offset, chunk
            )
            offset = appended["nextOffset"]
        committed = await dispatcher.commit_task_input(
            PEER, RUNTIME_ID, task["id"], INPUT_ID
        )
        assert committed["ready"] is True
        await asyncio.sleep(0)
        assert not executor.started.is_set()
        finished = await dispatcher.finish_task_inputs(
            PEER, RUNTIME_ID, task["id"]
        )
        assert finished["ready"] is True
        await asyncio.wait_for(executor.started.wait(), timeout=1)
        request = executor.requests[0]
        absolute_file = Path(request.workspace_path) / descriptor.relative_path
        assert absolute_file.is_absolute()
        assert absolute_file.read_bytes() == content
        assert str(absolute_file) in request.system_context
        assert "vision_analyze" in request.system_context
        assert len(request.attachments) == 1
        assert request.attachments[0].kind.value == "image"
        assert request.attachments[0].origin.value == "dsoftbus"
        assert request.attachments[0].path == str(absolute_file)
        assert request.attachments[0].mime_type == "image/png"

        executor.release.set()
        async def consume_terminal() -> None:
            while await subscription.next_event() is not None:
                pass

        await asyncio.wait_for(consume_terminal(), timeout=2)
        await asyncio.wait_for(
            dispatcher.acknowledge_task_result(
                PEER, RUNTIME_ID, task["id"]
            ),
            timeout=2,
        )
        assert not Path(request.workspace_path).parent.exists()
        await asyncio.wait_for(
            dispatcher.drain(asyncio.get_running_loop().time() + 1),
            timeout=2,
        )

    asyncio.run(scenario())


def test_a2a_media_part_round_trip_has_no_sender_absolute_path(
    tmp_path: Path,
) -> None:
    source = tmp_path / "private" / "photo.png"
    source.parent.mkdir()
    source.write_bytes(b"not-decoded-by-the-transport")
    prepared = OutboundTaskFileStore(
        DsoftbusWorkspace(tmp_path / "workspace"),
        uuid_factory=lambda: INPUT_ID,
    ).prepare(PEER, MESSAGE_ID, (str(source.resolve()),))
    descriptor = prepared.files[0][0]
    part = task_input_reference_part(descriptor)

    assert part["url"] == f"{TASK_INPUT_REFERENCE_PREFIX}{INPUT_ID}"
    assert str(source.resolve()) not in repr(dict(part))
    manifest = task_input_manifest_from_parts((part,))
    assert manifest is not None
    assert manifest["files"] == (descriptor.wire_value(),)


def test_a2a_media_part_uses_declared_hash_and_rejects_identity_mismatch() -> None:
    descriptor = _descriptor(b"content")
    part = dict(task_input_reference_part(descriptor))
    part["metadata"] = dict(part["metadata"])
    part["metadata"]["mclaw.sha256"] = "0" * 64

    manifest = task_input_manifest_from_parts((part,))
    assert manifest is not None
    assert manifest["files"][0]["sha256"] == "0" * 64

    part["url"] = f"{TASK_INPUT_REFERENCE_PREFIX}{MESSAGE_ID}"
    with pytest.raises(TaskFileError, match="TASK_INPUT_INVALID"):
        task_input_manifest_from_parts((part,))


def test_a2a_core_requires_file_extension_for_media_reference() -> None:
    descriptor = _descriptor(b"content", filename="image.png", media_type="image/png")
    part = dict(task_input_reference_part(descriptor))
    message = {
        "messageId": MESSAGE_ID,
        "role": "ROLE_USER",
        "parts": [{"text": "分析图片"}, part],
    }

    with pytest.raises(A2AError, match="EXTENSION_SUPPORT_REQUIRED"):
        validate_core_method("SendStreamingMessage", {"message": message})

    call = validate_core_method(
        "SendStreamingMessage",
        {
            "message": {
                **message,
                "extensions": [protocol.TASK_FILES_EXTENSION_URI],
            }
        },
    )
    normalized_part = call.params["message"]["parts"][1]
    assert normalized_part["url"] == f"{TASK_INPUT_REFERENCE_PREFIX}{INPUT_ID}"
    assert normalized_part["mediaType"] == "image/png"
