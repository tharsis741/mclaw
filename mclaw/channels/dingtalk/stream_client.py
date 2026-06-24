"""DingTalk Stream Mode and OpenAPI helpers."""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

import httpx

from mclaw.channels.base import SendResult
from mclaw.channels.dingtalk.config import DingTalkConfig
from mclaw.channels.dingtalk.formatter import normalize_markdown_for_dingtalk

logger = logging.getLogger(__name__)

try:
    import dingtalk_stream
    from dingtalk_stream import ChatbotMessage
    from dingtalk_stream.frames import AckMessage

    DINGTALK_STREAM_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency gate
    dingtalk_stream = None  # type: ignore[assignment]
    ChatbotMessage = None  # type: ignore[assignment]
    DINGTALK_STREAM_AVAILABLE = False

    class AckMessage:  # type: ignore[no-redef]
        STATUS_OK = 200
        STATUS_SYSTEM_EXCEPTION = 500


try:
    from alibabacloud_dingtalk.robot_1_0 import (
        client as dingtalk_robot_client,
        models as dingtalk_robot_models,
    )
    from alibabacloud_tea_openapi import models as open_api_models
    from alibabacloud_tea_util import models as tea_util_models

    DINGTALK_ROBOT_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency gate
    dingtalk_robot_client = None  # type: ignore[assignment]
    dingtalk_robot_models = None  # type: ignore[assignment]
    open_api_models = None  # type: ignore[assignment]
    tea_util_models = None  # type: ignore[assignment]
    DINGTALK_ROBOT_AVAILABLE = False

MessageHandler = Callable[[Any], Awaitable[None]]


def check_dingtalk_requirements() -> dict[str, bool]:
    stream_api = False
    if DINGTALK_STREAM_AVAILABLE:
        stream_api = all(
            [
                hasattr(dingtalk_stream, "DingTalkStreamClient"),
                hasattr(dingtalk_stream, "Credential"),
                hasattr(ChatbotMessage, "from_dict"),
                hasattr(ChatbotMessage, "TOPIC"),
            ]
        )

    robot_api = False
    if DINGTALK_ROBOT_AVAILABLE:
        robot_api = all(
            hasattr(dingtalk_robot_client.Client, name)
            for name in (
                "robot_message_file_download_with_options_async",
                "robot_reply_emotion_with_options_async",
                "robot_recall_emotion_with_options_async",
            )
        ) and all(
            hasattr(dingtalk_robot_models, name)
            for name in (
                "RobotMessageFileDownloadRequest",
                "RobotMessageFileDownloadHeaders",
                "RobotReplyEmotionRequest",
                "RobotReplyEmotionRequestTextEmotion",
                "RobotReplyEmotionHeaders",
                "RobotRecallEmotionRequest",
                "RobotRecallEmotionRequestTextEmotion",
                "RobotRecallEmotionHeaders",
            )
        )
    return {
        "dingtalk_stream": DINGTALK_STREAM_AVAILABLE,
        "httpx": True,
        "robot_sdk": DINGTALK_ROBOT_AVAILABLE,
        "stream_api": stream_api,
        "robot_api": robot_api,
    }


class DingTalkClient:
    def __init__(self, config: DingTalkConfig) -> None:
        self.config = config
        self.http: httpx.AsyncClient | None = None
        self.stream_client: Any = None
        self.robot_sdk: Any = None
        self._bg_tasks: set[asyncio.Task] = set()

    async def open(self) -> None:
        if not DINGTALK_STREAM_AVAILABLE:
            raise RuntimeError("dingtalk-stream is not installed. Install dingtalk-stream>=0.20.")
        self.http = httpx.AsyncClient(timeout=30.0)
        credential = dingtalk_stream.Credential(self.config.client_id, self.config.client_secret)
        self.stream_client = dingtalk_stream.DingTalkStreamClient(credential)
        if DINGTALK_ROBOT_AVAILABLE:
            sdk_config = open_api_models.Config()
            sdk_config.protocol = "https"
            sdk_config.region_id = "central"
            self.robot_sdk = dingtalk_robot_client.Client(sdk_config)

    def register_handler(self, *, loop: asyncio.AbstractEventLoop, handler: MessageHandler) -> None:
        if not self.stream_client:
            raise RuntimeError("DingTalk stream client is not opened")
        incoming = _IncomingHandler(handler=handler, loop=loop)
        self.stream_client.register_callback_handler(ChatbotMessage.TOPIC, incoming)

    async def start_stream(self) -> None:
        if not self.stream_client:
            raise RuntimeError("DingTalk stream client is not opened")
        await self.stream_client.start()

    async def close(self) -> None:
        timeout = max(0.1, float(getattr(self.config, "close_timeout_seconds", 3.0) or 3.0))
        websocket = getattr(self.stream_client, "websocket", None) if self.stream_client else None
        if websocket is not None:
            try:
                await asyncio.wait_for(websocket.close(), timeout=timeout)
            except (asyncio.TimeoutError, Exception) as exc:
                logger.debug("dingtalk websocket close failed: %s", exc)
        if self.stream_client is not None and hasattr(self.stream_client, "close"):
            try:
                await asyncio.wait_for(asyncio.to_thread(self.stream_client.close), timeout=timeout)
            except asyncio.TimeoutError:
                logger.debug("dingtalk stream client close timed out")
            except Exception:
                pass
        for task in list(self._bg_tasks):
            task.cancel()
        if self._bg_tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*self._bg_tasks, return_exceptions=True), timeout=timeout)
            except asyncio.TimeoutError:
                logger.debug("dingtalk background task close timed out")
            self._bg_tasks.clear()
        if self.http is not None:
            await self.http.aclose()
            self.http = None
        self.stream_client = None
        self.robot_sdk = None

    async def send_markdown(self, *, session_webhook: str, text: str, title: str = "M-Claw") -> SendResult:
        if not self.http:
            return SendResult(success=False, error="HTTP client not initialized")
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": normalize_markdown_for_dingtalk(text),
            },
        }
        try:
            response = await self.http.post(session_webhook, json=payload, timeout=15.0)
            if response.status_code < 300:
                return SendResult(success=True, message_id=uuid.uuid4().hex[:12])
            return SendResult(success=False, error=f"HTTP {response.status_code}: {response.text[:200]}")
        except httpx.TimeoutException:
            return SendResult(success=False, error="Timeout sending message to DingTalk")
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

    async def upload_media(self, *, file_path: str, media_type: str = "image") -> SendResult:
        if not self.http:
            return SendResult(success=False, error="HTTP client not initialized")
        token = await self.get_access_token()
        if not token:
            return SendResult(success=False, error="No DingTalk access token")
        path = Path(file_path)
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        try:
            with path.open("rb") as media_file:
                response = await self.http.post(
                    "https://oapi.dingtalk.com/media/upload",
                    params={"access_token": token, "type": media_type},
                    files={"media": (path.name, media_file, mime_type)},
                    timeout=60.0,
                )
            if response.status_code >= 300:
                return SendResult(success=False, error=f"HTTP {response.status_code}: {response.text[:200]}")
            data = response.json()
            if int(data.get("errcode", 0) or 0) != 0:
                return SendResult(success=False, error=f"{data.get('errcode')}: {data.get('errmsg')}")
            media_id = str(data.get("media_id") or "")
            if not media_id:
                return SendResult(success=False, error="DingTalk media upload did not return media_id")
            return SendResult(success=True, message_id=media_id)
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

    async def send_group_robot_message(
        self,
        *,
        open_conversation_id: str,
        msg_key: str,
        msg_param: dict[str, Any],
    ) -> SendResult:
        if not self.http:
            return SendResult(success=False, error="HTTP client not initialized")
        token = await self.get_access_token()
        if not token:
            return SendResult(success=False, error="No DingTalk access token")
        payload = {
            "msgParam": json.dumps(msg_param, ensure_ascii=False),
            "msgKey": msg_key,
            "openConversationId": open_conversation_id,
            "robotCode": self.config.robot_code,
        }
        try:
            response = await self.http.post(
                "https://api.dingtalk.com/v1.0/robot/groupMessages/send",
                headers={"x-acs-dingtalk-access-token": token},
                json=payload,
                timeout=30.0,
            )
            if response.status_code >= 300:
                return SendResult(success=False, error=f"HTTP {response.status_code}: {response.text[:200]}")
            data = response.json()
            process_query_key = str(data.get("processQueryKey") or "")
            if not process_query_key and data.get("code"):
                return SendResult(success=False, error=f"{data.get('code')}: {data.get('message') or data}")
            return SendResult(success=True, message_id=process_query_key or uuid.uuid4().hex[:12])
        except httpx.TimeoutException:
            return SendResult(success=False, error="Timeout sending DingTalk group robot message")
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

    async def send_oto_robot_message(
        self,
        *,
        user_id: str,
        msg_key: str,
        msg_param: dict[str, Any],
    ) -> SendResult:
        if not self.http:
            return SendResult(success=False, error="HTTP client not initialized")
        token = await self.get_access_token()
        if not token:
            return SendResult(success=False, error="No DingTalk access token")
        payload = {
            "robotCode": self.config.robot_code,
            "userIds": [user_id],
            "msgKey": msg_key,
            "msgParam": json.dumps(msg_param, ensure_ascii=False),
        }
        try:
            response = await self.http.post(
                "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend",
                headers={"x-acs-dingtalk-access-token": token},
                json=payload,
                timeout=30.0,
            )
            if response.status_code >= 300:
                return SendResult(success=False, error=f"HTTP {response.status_code}: {response.text[:200]}")
            data = response.json()
            invalid = [str(item) for item in data.get("invalidStaffIdList") or []]
            flow_controlled = [str(item) for item in data.get("flowControlledStaffIdList") or []]
            if user_id in invalid:
                return SendResult(success=False, error=f"DingTalk userId is invalid for OTO send: {user_id}")
            if user_id in flow_controlled:
                return SendResult(success=False, error=f"DingTalk OTO send is flow controlled for userId: {user_id}")
            process_query_key = str(data.get("processQueryKey") or "")
            if not process_query_key and data.get("code"):
                return SendResult(success=False, error=f"{data.get('code')}: {data.get('message') or data}")
            return SendResult(success=True, message_id=process_query_key or uuid.uuid4().hex[:12])
        except httpx.TimeoutException:
            return SendResult(success=False, error="Timeout sending DingTalk OTO robot message")
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

    async def download_bytes(self, url: str, *, timeout_seconds: float = 60.0) -> bytes:
        if not self.http:
            raise RuntimeError("HTTP client not initialized")
        response = await self.http.get(url, timeout=timeout_seconds)
        response.raise_for_status()
        return response.content

    async def get_access_token(self) -> str | None:
        if not self.stream_client:
            return None
        try:
            return await asyncio.to_thread(self.stream_client.get_access_token)
        except Exception as exc:
            logger.debug("dingtalk get_access_token failed: %s", exc)
            return None

    async def fetch_download_url(self, *, download_code: str, robot_code: str = "") -> str | None:
        if not self.robot_sdk:
            return None
        token = await self.get_access_token()
        if not token:
            return None
        try:
            request = dingtalk_robot_models.RobotMessageFileDownloadRequest(
                download_code=download_code,
                robot_code=robot_code or self.config.robot_code,
            )
            headers = dingtalk_robot_models.RobotMessageFileDownloadHeaders(
                x_acs_dingtalk_access_token=token,
            )
            runtime = tea_util_models.RuntimeOptions()
            response = await self.robot_sdk.robot_message_file_download_with_options_async(request, headers, runtime)
            body = response.body if response else None
            return getattr(body, "download_url", None) if body else None
        except Exception as exc:
            logger.warning("dingtalk downloadCode resolve failed: %s", exc)
            return None

    def spawn_bg(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def send_reaction(
        self,
        *,
        open_msg_id: str,
        open_conversation_id: str,
        emoji_name: str,
        recall: bool = False,
    ) -> None:
        if not self.robot_sdk or not open_msg_id or not open_conversation_id:
            return
        token = await self.get_access_token()
        if not token:
            return
        try:
            kwargs = {
                "robot_code": self.config.robot_code,
                "open_msg_id": open_msg_id,
                "open_conversation_id": open_conversation_id,
                "emotion_type": 2,
                "emotion_name": emoji_name,
            }
            runtime = tea_util_models.RuntimeOptions()
            if recall:
                kwargs["text_emotion"] = dingtalk_robot_models.RobotRecallEmotionRequestTextEmotion(
                    emotion_id="2659900",
                    emotion_name=emoji_name,
                    text=emoji_name,
                    background_id="im_bg_1",
                )
                request = dingtalk_robot_models.RobotRecallEmotionRequest(**kwargs)
                headers = dingtalk_robot_models.RobotRecallEmotionHeaders(x_acs_dingtalk_access_token=token)
                await self.robot_sdk.robot_recall_emotion_with_options_async(request, headers, runtime)
            else:
                kwargs["text_emotion"] = dingtalk_robot_models.RobotReplyEmotionRequestTextEmotion(
                    emotion_id="2659900",
                    emotion_name=emoji_name,
                    text=emoji_name,
                    background_id="im_bg_1",
                )
                request = dingtalk_robot_models.RobotReplyEmotionRequest(**kwargs)
                headers = dingtalk_robot_models.RobotReplyEmotionHeaders(x_acs_dingtalk_access_token=token)
                await self.robot_sdk.robot_reply_emotion_with_options_async(request, headers, runtime)
        except Exception:
            logger.debug("dingtalk reaction failed", exc_info=True)


class _IncomingHandler(dingtalk_stream.ChatbotHandler if DINGTALK_STREAM_AVAILABLE else object):
    def __init__(self, *, handler: MessageHandler, loop: asyncio.AbstractEventLoop) -> None:
        if DINGTALK_STREAM_AVAILABLE:
            super().__init__()
        self._handler = handler
        self._loop = loop

    def pre_start(self) -> None:
        return

    async def process(self, message: Any):
        try:
            data = getattr(message, "data", message)
            if isinstance(data, str):
                data = json.loads(data)
            if isinstance(data, dict):
                chatbot_msg = ChatbotMessage.from_dict(data) if DINGTALK_STREAM_AVAILABLE else SimpleNamespace()
                _patch_raw_payload_fields(chatbot_msg, data)
            else:
                chatbot_msg = ChatbotMessage.from_dict(data) if DINGTALK_STREAM_AVAILABLE else data
            self._schedule(self._safe_handle(chatbot_msg))
        except Exception:
            logger.exception("dingtalk incoming message preparation failed")
            return AckMessage.STATUS_SYSTEM_EXCEPTION, "error"
        return AckMessage.STATUS_OK, "OK"

    def _schedule(self, coro) -> None:
        if not self._loop.is_closed():
            self._loop.create_task(coro)
            return
        try:
            asyncio.create_task(coro)
        except RuntimeError:
            coro.close()
            raise

    async def _safe_handle(self, chatbot_msg: Any) -> None:
        try:
            await self._handler(chatbot_msg)
        except Exception:
            logger.exception("dingtalk incoming message handler failed")


def _patch_raw_payload_fields(chatbot_msg: Any, data: dict[str, Any]) -> None:
    """Keep raw DingTalk fields that dingtalk-stream may not map consistently."""
    setattr(chatbot_msg, "_raw_payload", data)
    mappings = {
        "msgId": "message_id",
        "msg_id": "message_id",
        "messageId": "message_id",
        "message_id": "message_id",
        "msgtype": "message_type",
        "messageType": "message_type",
        "message_type": "message_type",
        "conversationId": "conversation_id",
        "conversation_id": "conversation_id",
        "conversationType": "conversation_type",
        "conversation_type": "conversation_type",
        "conversationTitle": "conversation_title",
        "conversation_title": "conversation_title",
        "senderId": "sender_id",
        "sender_id": "sender_id",
        "senderStaffId": "sender_staff_id",
        "sender_staff_id": "sender_staff_id",
        "senderNick": "sender_nick",
        "sender_nick": "sender_nick",
        "robotCode": "robot_code",
        "robot_code": "robot_code",
        "sessionWebhook": "session_webhook",
        "session_webhook": "session_webhook",
        "sessionWebhookExpiredTime": "session_webhook_expired_time",
        "session_webhook_expired_time": "session_webhook_expired_time",
        "isInAtList": "is_in_at_list",
        "is_in_at_list": "is_in_at_list",
    }
    for raw_key, attr_name in mappings.items():
        if raw_key not in data:
            continue
        current = getattr(chatbot_msg, attr_name, None)
        if current in (None, "", False):
            setattr(chatbot_msg, attr_name, data[raw_key])
    if isinstance(data.get("content"), dict):
        current_content = getattr(chatbot_msg, "content", None)
        if current_content in (None, ""):
            setattr(chatbot_msg, "content", data["content"])
