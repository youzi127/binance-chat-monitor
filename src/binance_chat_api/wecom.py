from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx


class WeComError(RuntimeError):
    pass


def _validate_webhook_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "qyapi.weixin.qq.com":
        raise ValueError("WeCom webhook must use HTTPS on qyapi.weixin.qq.com")
    if parsed.path != "/cgi-bin/webhook/send" or not parse_qs(parsed.query).get("key"):
        raise ValueError("invalid WeCom robot webhook path or key")


def _format_time(timestamp_ms: int) -> str:
    value = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).astimezone()
    return value.strftime("%Y-%m-%d %H:%M:%S")


def format_chat_notification(
    *,
    session_name: str,
    sender: str,
    content: str,
    message_time_ms: int,
    received_at_ms: int,
    max_length: int = 2048,
) -> str:
    stamp = datetime.fromtimestamp(message_time_ms / 1000, tz=UTC).astimezone().strftime("%H:%M:%S")
    prefix = f"【聊天室-{session_name}】1 条消息\n- {sender} · {stamp}:\n"
    available = max(0, max_length - len(prefix))
    return prefix + content[:available]


class WeComNotifier:
    def __init__(
        self,
        webhook_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        _validate_webhook_url(webhook_url)
        self._url = webhook_url
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0),
            transport=transport,
            follow_redirects=False,
        )

    async def send_text(self, text: str) -> None:
        try:
            response = await self._client.post(
                self._url,
                json={"msgtype": "markdown", "markdown": {"content": text[:2048]}},
            )
        except httpx.HTTPError as exc:
            raise WeComError(f"WeCom transport error: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise WeComError(f"WeCom HTTP {response.status_code}")
        try:
            payload: dict[str, Any] = response.json()
        except ValueError as exc:
            raise WeComError("WeCom returned non-JSON data") from exc
        if payload.get("errcode") != 0:
            raise WeComError(f"WeCom rejected message (errcode={payload.get('errcode')})")

    async def close(self) -> None:
        await self._client.aclose()

