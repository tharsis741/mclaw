# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import hashlib
import logging

from mclaw.application.diagnostics import configure_worker_logging


def test_worker_log_records_stages_and_redacts_errors_without_dumping_messages(tmp_path, monkeypatch):
    monkeypatch.setenv("FIXTURE_API_KEY", "environment-credential-value")
    secrets = {"initial-credential-value"}
    run = ["first-run"]
    root = logging.getLogger()
    original_level = root.level
    handler = configure_worker_logging(tmp_path, "session-one", secrets=lambda: secrets.copy(), run_id=lambda: run[0])
    logger = logging.getLogger("mclaw.agent.core")
    try:
        logger.info("[MODEL REQUEST] provider=fixture model=vision images=1")
        logger.info("[SYSTEM PROMPT]\nprivate-system-content")
        logger.info("[MESSAGES BEFORE API CALL] private-conversation-content")
        secrets.add("new-credential-value")
        run[0] = "second-run"
        try:
            raise ValueError('initial-credential-value new-credential-value environment-credential-value\n'
                             'Authorization: Bearer opaque-token-value\n{"api_key": "json-key-value"}\n'
                             'Authorization: Basic dXNlcjpwYXNz\nhttps://user:pass@example.test/v1\n'
                             'Set-Cookie: session=private-cookie-value; Secure\n'
                             'data:image/png;base64,aW1hZ2VkYXRh')
        except ValueError:
            logger.exception("Model failed")
    finally:
        root.removeHandler(handler)
        handler.close()
        root.setLevel(original_level)
    path = tmp_path / "logs" / "sessions" / (hashlib.sha256(b"session-one").hexdigest() + ".log")
    text = path.read_text(encoding="utf-8")
    assert "session=session-one run=first-run" in text and "run=second-run" in text
    assert "[MODEL REQUEST]" in text and "Traceback" in text and "ValueError" in text
    for secret in [*secrets, "environment-credential-value", "opaque-token-value", "json-key-value", "aW1hZ2VkYXRh",
                   "dXNlcjpwYXNz", "user:pass", "private-cookie-value", "private-system-content", "private-conversation-content"]:
        assert secret not in text
    assert "<redacted>" in text and "<image data>" in text


def test_worker_logs_use_safe_session_paths_and_rotate(tmp_path):
    root = logging.getLogger()
    original_level = root.level
    handler = configure_worker_logging(tmp_path, "../session", secrets=lambda: (), run_id=lambda: "run")
    handler.maxBytes = 1024
    try:
        for index in range(8):
            logging.getLogger("mclaw.test").warning("sample %d %s", index, "x" * 600)
    finally:
        root.removeHandler(handler)
        handler.close()
        root.setLevel(original_level)
    paths = list((tmp_path / "logs" / "sessions").iterdir())
    assert len(paths) == 3
    assert all(path.name.startswith(hashlib.sha256(b"../session").hexdigest()) for path in paths)


def test_unavailable_diagnostic_directory_does_not_block_the_worker(tmp_path):
    (tmp_path / "logs").write_text("not a directory", encoding="utf-8")
    assert configure_worker_logging(tmp_path, "session", secrets=lambda: (), run_id=lambda: "") is None
