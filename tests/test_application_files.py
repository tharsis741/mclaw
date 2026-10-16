# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path
import asyncio
import json

import pytest
from starlette.testclient import TestClient

from mclaw.application.files import FileService, MAX_PREVIEW_BYTES, attachment_context
from mclaw.application.sessions import ApplicationError, SessionService
from mclaw.runtime.paths import PathPolicy
from mclaw.runtime.attachments import image_question, refresh_attachment_context
from mclaw.web.app import create_app


@pytest.fixture
def files(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("MCLAW_HOME", str(home))
    policy = PathPolicy(mclaw_home=home)
    monkeypatch.setattr(FileService, "_policy", lambda _self: policy)
    return FileService(home)


def test_attachment_context_uses_supplied_contents_and_keeps_each_source():
    selected = [
        {"name": "photo.png", "path": "/inputs/photo.png", "source": "upload", "mime_type": "image/png"},
        {"name": "notes.txt", "path": "/work/notes.txt", "source": "local", "mime_type": "text/plain"},
    ]
    context = attachment_context(selected)
    assert "already supplied in this conversation directly" in context
    assert "when the requested information is not already available" in context
    assert "before using" not in context
    assert "Uploaded files are copies" not in context
    assert json.loads(context.splitlines()[-1]) == selected
    assert image_question("Compare these" + context) == "Compare these"
    assert refresh_attachment_context(context) == context
    assert attachment_context([]) == ""


@pytest.mark.parametrize("text", [
    "Please explain: Read the relevant files with available tools before using their contents.",
    "My [Attached files] example is not an attachment manifest.",
    "\n\n[Attached files]\nUser-provided instructions\n[]",
    "\n\n[Attached files]\nThe user attached these local files. Read the relevant files with available tools before using their contents. "
    "Uploaded files are copies; local references retain their original paths.\n[{\"name\": \"incomplete\"}]",
])
def test_attachment_refresh_preserves_unrecognized_user_text(text):
    assert refresh_attachment_context(text) == text
    assert image_question(text) == text


def test_upload_keeps_same_names_separate_and_binds_to_session(files):
    first = files.upload("session_a", "材料.md", "第一份".encode())
    second = files.upload("session_a", "材料.md", "第二份".encode())
    assert first["path"] != second["path"]
    assert files.preview("session_a", first["id"])["content"] == "第一份"
    assert files.preview("session_a", second["id"])["content"] == "第二份"
    with pytest.raises(ApplicationError) as error:
        files.resolve("session_b", first["id"])
    assert error.value.code == "FILE_NOT_FOUND"
    assert FileService(files.home).get("session_a", first["id"]) == first


def test_attachment_image_limit_preserves_mixed_files_and_counts_unique_selections(files):
    pictures = [files.upload("session_a", f"image-{index}.png", b"image") for index in range(9)]
    texts = [files.upload("session_a", f"text-{index}.txt", b"text") for index in range(12)]
    ids = lambda records: [item["id"] for item in records]
    assert len(files.snapshots("session_a", ids(pictures[:8] + texts))) == 20
    assert len(files.snapshots("session_a", ids(pictures[:8] + pictures[:1]))) == 8
    with pytest.raises(ApplicationError, match="8 张图片") as error:
        files.snapshots("session_a", ids(pictures))
    assert error.value.code == "TOO_MANY_IMAGES"


def test_selected_upload_has_a_session_bound_shareable_copy_and_original_is_preserved(files, monkeypatch):
    from types import SimpleNamespace
    from mclaw.dsoftbus.tools import _resolve_task_input_paths
    from mclaw.runtime.manager import RuntimeManager

    uploaded = files.upload("session_a", "材料.md", "资料正文".encode())
    local = files.home.parent / "local.md"
    local.write_text("local", encoding="utf-8")
    reference = files.link("session_a", str(local))
    inputs = files.task_inputs("session_a", [uploaded, reference])
    assert inputs[0]["path"] != uploaded["path"]
    assert Path(inputs[0]["path"]).read_bytes() == Path(uploaded["path"]).read_bytes()
    assert files.task_inputs("session_a", [uploaded])[0]["path"] == inputs[0]["path"]
    assert inputs[1]["path"] == str(local)
    monkeypatch.setattr(RuntimeManager, "_current", SimpleNamespace(paths=files._policy()))
    assert _resolve_task_input_paths([inputs[0]["path"]], None) == (inputs[0]["path"],)
    with pytest.raises(Exception) as error:
        _resolve_task_input_paths([uploaded["path"]], None)
    assert error.value.code == "SOURCE_PATH_FORBIDDEN"
    with pytest.raises(ApplicationError):
        files.task_inputs("session_b", [uploaded])
    assert files.get("session_a", uploaded["id"]) == uploaded


def test_task_input_cannot_override_record_path_or_overwrite_a_changed_copy(files):
    record = files.upload("session_a", "notes.md", b"selected content")
    forged = {**record, "path": str(files.home / ".env")}
    copied = files.task_inputs("session_a", [forged])[0]
    assert Path(copied["path"]).read_bytes() == b"selected content"
    Path(copied["path"]).write_text("later changes")
    with pytest.raises(ApplicationError) as error:
        files.task_inputs("session_a", [record])
    assert error.value.code == "FILE_CHANGED"
    assert Path(copied["path"]).read_text() == "later changes"


def test_attachment_origin_uses_system_device_name(files, monkeypatch):
    monkeypatch.setattr("mclaw.application.files.get_local_device_name", lambda: "Kaihong BotBook")
    record = files.upload("session_a", "材料.md", b"test")
    assert record["device"] == "Kaihong BotBook"
    assert FileService(files.home).get("session_a", record["id"])["device"] == "Kaihong BotBook"


@pytest.mark.parametrize("name", ["../data.txt", "x\\data.txt", "x:y.txt", ".env", ".env.local", "CON.txt", "x. ", ""])
def test_import_rejects_unsafe_names(files, name):
    with pytest.raises(ApplicationError):
        files.upload("session_a", name, b"test")
    assert not files.root.exists()


def test_local_file_reference_preserves_original_and_reports_missing_file(files, tmp_path):
    path = tmp_path / "document.txt"
    path.write_text("original", encoding="utf-8")
    record = files.link("session_a", str(path), str(tmp_path))
    assert record["path"] == str(path) and record["source"] == "local"
    assert path.read_text() == "original"
    selected = files.snapshots("session_a", [record["id"], record["id"]])
    assert len(selected) == 1 and str(path).replace("\\", "\\\\") in attachment_context(selected)
    path.unlink()
    with pytest.raises(ApplicationError) as error:
        files.snapshots("session_a", [record["id"]])
    assert error.value.code == "FILE_UNAVAILABLE"


def test_file_browser_and_references_hide_credentials_and_runtime_data(files, tmp_path):
    credential = tmp_path / ".env.local"
    credential.write_text("KEY=test")
    private = files.home / "config.yaml"
    private.write_text("private: true")
    for target in (credential, private):
        with pytest.raises(ApplicationError) as error:
            files.link("session_a", str(target))
        assert error.value.code == "FILE_ACCESS_DENIED"
    listing = files.browse(str(tmp_path))
    assert not any(item["name"] in {"home", ".env.local"} for item in listing["entries"])


def test_task_files_can_be_written_browsed_and_previewed(files, monkeypatch):
    from types import SimpleNamespace
    from mclaw.runtime.manager import RuntimeManager
    from mclaw.tools import file_operations

    policy = PathPolicy(mclaw_home=files.home)
    monkeypatch.setattr(RuntimeManager, "_current", SimpleNamespace(paths=policy))
    root = files.home / "_temp"
    path = root / "session_a" / "report.md"
    file_operations.write_file(str(path), "task file")
    record = files.link("session_a", str(path), str(path.parent))
    assert files.preview("session_a", record["id"])["content"] == "task file"
    assert files.resolve("session_a", record["id"])[1] == path
    assert files.browse(str(path.parent))["entries"][0]["name"] == "report.md"
    assert files.browse(str(root))["parent"] == str(root)
    (path.parent / ".env").write_text("fake secret", encoding="utf-8")
    with pytest.raises(ApplicationError):
        files.link("session_a", str(path.parent / ".env"))
    with pytest.raises(PermissionError):
        file_operations.write_file(str(root / ".." / "config.yaml"), "blocked")


def test_preview_limits_text_and_never_marks_html_svg_as_active_content(files):
    large = files.upload("session_a", "long.md", b"a" * (MAX_PREVIEW_BYTES + 3))
    preview = files.preview("session_a", large["id"])
    assert preview["kind"] == "markdown" and preview["truncated"]
    assert len(preview["content"]) == MAX_PREVIEW_BYTES
    for name in ("page.html", "icon.svg"):
        record = files.upload("session_a", name, b"<script>alert(1)</script>")
        assert files.preview("session_a", record["id"])["kind"] == "text"
    binary = files.upload("session_a", "archive.bin", b"\0\xff")
    assert files.preview("session_a", binary["id"])["kind"] == "download"


def test_file_http_requires_session_control_for_upload_preview_and_download(files):
    headers = {"Authorization": "Bearer test-local", "X-MClaw-Client": "window-a"}
    with TestClient(create_app(files.home, "test-local"), base_url="http://127.0.0.1") as client:
        session = client.post("/api/v1/sessions", headers=headers, json={}).json()
        url = f"/api/v1/sessions/{session['id']}/files"
        assert client.post(url, params={"name": "input.txt"}, content=b"input").status_code == 403
        imported = client.post(url, headers=headers, params={"name": "input.txt"}, content=b"input")
        assert imported.status_code == 201
        file_id = imported.json()["id"]
        assert client.get(f"{url}/{file_id}/preview", headers=headers).json()["content"] == "input"
        download = client.get(f"{url}/{file_id}/content", headers=headers)
        assert download.content == b"input" and "attachment" in download.headers["content-disposition"]
        rival = headers | {"X-MClaw-Client": "window-b"}
        assert client.get(f"{url}/{file_id}/content", headers=rival).status_code == 409


def test_managed_task_file_http_preview_and_download(files):
    headers = {"Authorization": "Bearer test-local", "X-MClaw-Client": "window-a"}
    with TestClient(create_app(files.home, "test-local"), base_url="http://127.0.0.1", headers=headers) as client:
        session = client.post("/api/v1/sessions", json={}).json()
        workspace = files.home / "_temp" / session["id"]
        workspace.mkdir(parents=True)
        path = workspace / "report.txt"
        path.write_text("generated task file", encoding="utf-8")
        record = files.link(session["id"], str(path), str(workspace))
        url = f"/api/v1/sessions/{session['id']}/files/{record['id']}"
        assert client.get(f"{url}/preview").json()["content"] == "generated task file"
        response = client.get(f"{url}/content")
        assert response.content == b"generated task file"
        assert "attachment" in response.headers["content-disposition"]


@pytest.mark.asyncio
async def test_attachment_acceptance_draft_queue_and_idempotency(files, tmp_path):
    service = SessionService(files.home)
    output = tmp_path / "outputs"
    output.mkdir()
    service.store.set_setting("output_root", str(output))
    try:
        sid = service.create("owner")["id"]
        other = service.create("owner")["id"]
        service.stop(sid, "owner")
        record = files.upload(sid, "task.txt", b"task material")
        service.patch(sid, "owner", {"draft": "read this", "draft_attachments": [record["id"]]})
        assert service.snapshot(sid)["draft_attachments"] == [record["id"]]
        with pytest.raises(ApplicationError):
            service.submit(other, "owner", "wrong session", "wrong", attachments=[record["id"]])
        accepted = service.submit(sid, "owner", "read this", "request", attachments=[record["id"]])
        assert accepted["draft_attachments"] == []
        task = accepted["queue"][0]
        assert task["attachments"] == [record]
        edited = service.edit_queue(sid, "owner", [{"id": task["id"], "content": "read the first paragraph"}])
        assert edited["queue"][0]["attachments"] == [record]
        Path(record["path"]).unlink()
        duplicate = service.submit(sid, "owner", "read this", "request", attachments=[record["id"]])
        assert len(duplicate["queue"]) == 1
        removed = service.edit_queue(sid, "owner", [{"id": task["id"], "content": "different task", "attachments": []}])
        assert removed["queue"][0]["attachments"] == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_image_limit_rejects_draft_submission_and_queue_changes_atomically(files, tmp_path):
    service = SessionService(files.home)
    output = tmp_path / "outputs"
    output.mkdir()
    service.store.set_setting("output_root", str(output))
    try:
        sid = service.create("owner")["id"]
        service.stop(sid, "owner")
        images = [files.upload(sid, f"image-{index}.png", b"image")["id"] for index in range(9)]
        service.patch(sid, "owner", {"draft": "original draft", "draft_attachments": images[:8]})
        with pytest.raises(ApplicationError, match="8 张图片"):
            service.patch(sid, "owner", {"draft": "rejected draft", "draft_attachments": images})
        assert service.snapshot(sid)["draft"] == "original draft"
        assert service.snapshot(sid)["draft_attachments"] == images[:8]
        with pytest.raises(ApplicationError, match="8 张图片"):
            service.submit(sid, "owner", "Read images", "request", attachments=images)
        assert not service.snapshot(sid)["queue"]
        # The rejected request ID remains usable after correcting the selection.
        queued = service.submit(sid, "owner", "Read images", "request", attachments=images[:8])["queue"]
        with pytest.raises(ApplicationError, match="8 张图片"):
            service.edit_queue(sid, "owner", [{"id": queued[0]["id"], "content": "Changed", "attachments": images}])
        assert service.snapshot(sid)["queue"] == queued
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_steer_and_resume_keep_their_own_attachments(files, tmp_path):
    class Runner:
        def __init__(self, _session):
            self.results = asyncio.Queue()
            self.commands = []
        async def run(self, _task, _emit):
            return await self.results.get()
        def send(self, command):
            self.commands.append(command)
            if command["type"] == "stop":
                self.results.put_nowait({"completed": False, "interrupted": True})
        async def close(self):
            pass

    async def settle():
        for _ in range(5):
            await asyncio.sleep(0)

    service = SessionService(files.home, runner_factory=Runner)
    output = tmp_path / "outputs"
    output.mkdir()
    service.store.set_setting("output_root", str(output))
    try:
        sid = service.create("owner")["id"]
        record = files.upload(sid, "support.txt", b"supporting text")
        service.submit(sid, "owner", "begin", "original")
        await settle()
        runner = service.runners[sid]
        service.submit(sid, "owner", "add detail", "steer", "steer", attachments=[record["id"]])
        assert runner.commands[-1]["attachments"] == [record]
        service.stop(sid, "owner")
        await settle()
        resumed = service.resume(sid, "owner", "continue with this", "resume", attachments=[record["id"]])
        assert resumed["queue"][0]["supplements"][-1]["attachments"] == [record]
        assert resumed["messages"][-1]["attachments"] == [record]
    finally:
        await settle()
        await service.close()
