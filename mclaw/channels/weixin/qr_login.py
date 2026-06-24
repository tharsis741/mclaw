"""Interactive iLink QR login for the Weixin channel."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from mclaw.channels.weixin.account_store import WeixinAccountStore
from mclaw.channels.weixin.ilink_client import ILINK_BASE_URL, ILinkClient

QR_TIMEOUT_MS = 35_000


class QRLoginClient(Protocol):
    async def get_bot_qrcode(self, *, bot_type: str = "3", timeout_ms: int = QR_TIMEOUT_MS) -> dict: ...
    async def get_qrcode_status(
        self,
        *,
        qrcode: str,
        base_url: str | None = None,
        timeout_ms: int = QR_TIMEOUT_MS,
    ) -> dict: ...


@dataclass(frozen=True)
class WeixinLoginCredentials:
    account_id: str
    token: str
    base_url: str
    user_id: str = ""


def _redirect_base_url(host: str) -> str:
    raw = host.strip().rstrip("/")
    if not raw:
        return ILINK_BASE_URL
    if raw.startswith(("http://", "https://")):
        return raw
    return f"https://{raw}"


def _render_qr(scan_data: str, print_fn: Callable[[str], None]) -> None:
    try:
        import qrcode

        qr = qrcode.QRCode()
        qr.add_data(scan_data)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except Exception as exc:
        print_fn(f"终端二维码渲染失败: {exc}。请打开上方链接并使用微信扫码。")


def _save_credentials(account_store: WeixinAccountStore, credentials: WeixinLoginCredentials) -> None:
    account_store.save_account(
        credentials.account_id,
        {
            "token": credentials.token,
            "base_url": credentials.base_url,
            "user_id": credentials.user_id,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )


async def qr_login(
    *,
    account_store: WeixinAccountStore | None = None,
    client: QRLoginClient | None = None,
    bot_type: str = "3",
    timeout_seconds: int = 480,
    poll_interval_seconds: float = 1.0,
    max_refreshes: int = 3,
    print_fn: Callable[[str], None] = print,
    render_qr: bool = True,
) -> WeixinLoginCredentials | None:
    """Run the Tencent iLink QR login flow and persist credentials on success."""

    account_store = account_store or WeixinAccountStore()
    owns_client = client is None
    if client is None:
        client = ILinkClient(base_url=ILINK_BASE_URL, token="", timeout_ms=QR_TIMEOUT_MS)
        await client.open()  # type: ignore[attr-defined]

    try:
        qr_response = await client.get_bot_qrcode(bot_type=bot_type, timeout_ms=QR_TIMEOUT_MS)
        qrcode_value = str(qr_response.get("qrcode") or "").strip()
        qrcode_url = str(qr_response.get("qrcode_img_content") or "").strip()
        if not qrcode_value:
            print_fn("微信扫码登录失败：iLink 响应缺少 qrcode。")
            return None

        _show_qr(qrcode_value=qrcode_value, qrcode_url=qrcode_url, print_fn=print_fn, render_qr=render_qr)

        deadline = time.monotonic() + timeout_seconds
        current_base_url = ILINK_BASE_URL
        refresh_count = 0

        while time.monotonic() < deadline:
            try:
                status_response = await client.get_qrcode_status(
                    qrcode=qrcode_value,
                    base_url=current_base_url,
                    timeout_ms=QR_TIMEOUT_MS,
                )
            except asyncio.TimeoutError:
                await asyncio.sleep(poll_interval_seconds)
                continue
            except Exception as exc:
                print_fn(f"微信扫码状态查询失败: {exc}")
                await asyncio.sleep(poll_interval_seconds)
                continue

            status = str(status_response.get("status") or "wait").strip()
            if status == "wait":
                print_fn(".")
            elif status == "scaned":
                print_fn("已扫码，请在微信中确认登录。")
            elif status == "scaned_but_redirect":
                redirect_host = str(status_response.get("redirect_host") or "").strip()
                if redirect_host:
                    current_base_url = _redirect_base_url(redirect_host)
            elif status == "expired":
                refresh_count += 1
                if refresh_count > max_refreshes:
                    print_fn("微信二维码多次过期，请重新运行登录。")
                    return None
                qr_response = await client.get_bot_qrcode(bot_type=bot_type, timeout_ms=QR_TIMEOUT_MS)
                qrcode_value = str(qr_response.get("qrcode") or "").strip()
                qrcode_url = str(qr_response.get("qrcode_img_content") or "").strip()
                if not qrcode_value:
                    print_fn("微信二维码刷新失败：iLink 响应缺少 qrcode。")
                    return None
                _show_qr(qrcode_value=qrcode_value, qrcode_url=qrcode_url, print_fn=print_fn, render_qr=render_qr)
            elif status == "confirmed":
                credentials = _credentials_from_status(status_response)
                if credentials is None:
                    print_fn("微信扫码已确认，但返回的凭据不完整。")
                    return None
                _save_credentials(account_store, credentials)
                print_fn(f"微信登录成功。account_id={credentials.account_id}")
                return credentials
            else:
                print_fn(f"微信扫码状态: {status}")

            await asyncio.sleep(poll_interval_seconds)

        print_fn("微信扫码登录已超时。")
        return None
    finally:
        if owns_client and hasattr(client, "close"):
            await client.close()  # type: ignore[attr-defined]


def _show_qr(
    *,
    qrcode_value: str,
    qrcode_url: str,
    print_fn: Callable[[str], None],
    render_qr: bool,
) -> None:
    scan_data = qrcode_url or qrcode_value
    print_fn("请使用微信扫描二维码并确认登录:")
    if qrcode_url:
        print_fn(qrcode_url)
    if render_qr:
        _render_qr(scan_data, print_fn)


def _credentials_from_status(response: dict) -> WeixinLoginCredentials | None:
    account_id = str(response.get("ilink_bot_id") or "").strip()
    token = str(response.get("bot_token") or "").strip()
    base_url = str(response.get("baseurl") or ILINK_BASE_URL).strip().rstrip("/")
    user_id = str(response.get("ilink_user_id") or "").strip()
    if not account_id or not token:
        return None
    return WeixinLoginCredentials(
        account_id=account_id,
        token=token,
        base_url=base_url,
        user_id=user_id,
    )
