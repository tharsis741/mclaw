# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Minimal Tencent iLink client used by the Weixin channel."""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
import struct
from typing import Any

import httpx

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
EP_SEND_TYPING = "ilink/bot/sendtyping"
EP_GET_CONFIG = "ilink/bot/getconfig"
EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"
EP_GET_UPLOAD_URL = "ilink/bot/getuploadurl"

ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.2.0"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0

MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2
ITEM_TEXT = 1
TYPING_START = 1
TYPING_STOP = 2


def _random_wechat_uin() -> str:
    value = struct.unpack(">I", secrets.token_bytes(4))[0]
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def _json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _headers(token: str | None, body: str) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Content-Length": str(len(body.encode("utf-8"))),
        "X-WECHAT-UIN": _random_wechat_uin(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


class ILinkClient:
    def __init__(self, *, base_url: str, token: str, timeout_ms: int = 15000) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_ms = timeout_ms
        self._client: httpx.AsyncClient | None = None
        self._client_loop: asyncio.AbstractEventLoop | None = None

    async def __aenter__(self) -> "ILinkClient":
        await self.open()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    async def open(self) -> None:
        loop = asyncio.get_running_loop()
        if self._client is not None and self._client_loop is loop and not loop.is_closed():
            return
        if self._client is not None:
            client = self._client
            self._client = None
            self._client_loop = None
            try:
                await client.aclose()
            except RuntimeError as exc:
                if "Event loop is closed" not in str(exc):
                    raise
        self._client = httpx.AsyncClient(timeout=None, trust_env=True)
        self._client_loop = loop

    async def close(self) -> None:
        client = self._client
        self._client = None
        self._client_loop = None
        if client is not None:
            await client.aclose()

    async def post(self, endpoint: str, payload: dict[str, Any], *, timeout_ms: int | None = None) -> dict[str, Any]:
        await self.open()
        assert self._client is not None
        body = _json_dumps({**payload, "base_info": {"channel_version": CHANNEL_VERSION}})
        response = await self._client.post(
            f"{self.base_url}/{endpoint}",
            content=body.encode("utf-8"),
            headers=_headers(self.token, body),
            timeout=(timeout_ms or self.timeout_ms) / 1000,
        )
        response.raise_for_status()
        return response.json()

    async def get(self, endpoint: str, *, timeout_ms: int | None = None) -> dict[str, Any]:
        await self.open()
        assert self._client is not None
        response = await self._client.get(
            f"{self.base_url}/{endpoint}",
            headers={
                "iLink-App-Id": ILINK_APP_ID,
                "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
            },
            timeout=(timeout_ms or self.timeout_ms) / 1000,
        )
        response.raise_for_status()
        return response.json()

    async def download_bytes(self, url: str, *, timeout_ms: int | None = None) -> bytes:
        await self.open()
        assert self._client is not None
        response = await self._client.get(url, timeout=(timeout_ms or self.timeout_ms) / 1000)
        response.raise_for_status()
        return response.content

    async def upload_bytes(self, url: str, data: bytes, *, timeout_ms: int | None = None) -> str:
        await self.open()
        assert self._client is not None
        response = await self._client.post(
            url,
            content=data,
            headers={"Content-Type": "application/octet-stream"},
            timeout=(timeout_ms or 120000) / 1000,
        )
        response.raise_for_status()
        encrypted_param = response.headers.get("x-encrypted-param")
        if not encrypted_param:
            raise RuntimeError(f"CDN upload missing x-encrypted-param header: {response.text[:200]}")
        return encrypted_param

    async def get_bot_qrcode(self, *, bot_type: str = "3", timeout_ms: int = 35000) -> dict[str, Any]:
        return await self.get(f"{EP_GET_BOT_QR}?bot_type={bot_type}", timeout_ms=timeout_ms)

    async def get_qrcode_status(
        self,
        *,
        qrcode: str,
        base_url: str | None = None,
        timeout_ms: int = 35000,
    ) -> dict[str, Any]:
        old_base_url = self.base_url
        if base_url:
            self.base_url = base_url.rstrip("/")
        try:
            return await self.get(f"{EP_GET_QR_STATUS}?qrcode={qrcode}", timeout_ms=timeout_ms)
        finally:
            self.base_url = old_base_url

    async def get_updates(self, sync_buf: str, *, timeout_ms: int) -> dict[str, Any]:
        try:
            return await self.post(
                EP_GET_UPDATES,
                {"get_updates_buf": sync_buf},
                timeout_ms=timeout_ms,
            )
        except httpx.TimeoutException:
            return {"ret": 0, "msgs": [], "get_updates_buf": sync_buf}

    async def send_text(
        self,
        *,
        to_user_id: str,
        text: str,
        client_id: str,
        context_token: str | None = None,
    ) -> dict[str, Any]:
        msg: dict[str, Any] = {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": client_id,
            "message_type": MSG_TYPE_BOT,
            "message_state": MSG_STATE_FINISH,
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
        }
        if context_token:
            msg["context_token"] = context_token
        return await self.post(EP_SEND_MESSAGE, {"msg": msg})

    async def send_item(
        self,
        *,
        to_user_id: str,
        item: dict[str, Any],
        client_id: str,
        context_token: str | None = None,
    ) -> dict[str, Any]:
        msg: dict[str, Any] = {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": client_id,
            "message_type": MSG_TYPE_BOT,
            "message_state": MSG_STATE_FINISH,
            "item_list": [item],
        }
        if context_token:
            msg["context_token"] = context_token
        return await self.post(EP_SEND_MESSAGE, {"msg": msg})

    async def get_upload_url(
        self,
        *,
        to_user_id: str,
        media_type: int,
        filekey: str,
        rawsize: int,
        rawfilemd5: str,
        filesize: int,
        aeskey_hex: str,
    ) -> dict[str, Any]:
        return await self.post(
            EP_GET_UPLOAD_URL,
            {
                "filekey": filekey,
                "media_type": media_type,
                "to_user_id": to_user_id,
                "rawsize": rawsize,
                "rawfilemd5": rawfilemd5,
                "filesize": filesize,
                "no_need_thumb": True,
                "aeskey": aeskey_hex,
            },
            timeout_ms=self.timeout_ms,
        )

    async def get_config(self, *, user_id: str, context_token: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"ilink_user_id": user_id}
        if context_token:
            payload["context_token"] = context_token
        return await self.post(EP_GET_CONFIG, payload)

    async def send_typing(self, *, user_id: str, typing_ticket: str, started: bool) -> None:
        await self.post(
            EP_SEND_TYPING,
            {
                "ilink_user_id": user_id,
                "typing_ticket": typing_ticket,
                "status": TYPING_START if started else TYPING_STOP,
            },
        )
