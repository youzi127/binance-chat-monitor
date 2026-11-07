import json

import httpx
import pytest

from binance_chat_api.wecom import WeComError, WeComNotifier, format_chat_notification


@pytest.mark.asyncio
async def test_wecom_sends_text_and_accepts_errcode_zero():
    observed = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        observed["json"] = json.loads(request.content)
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    notifier = WeComNotifier(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret-key",
        transport=httpx.MockTransport(handler),
    )
    await notifier.send_text("hello")
    assert observed["json"] == {"msgtype": "markdown", "markdown": {"content": "hello"}}
    await notifier.close()


@pytest.mark.asyncio
async def test_wecom_error_never_contains_webhook_secret():
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errcode": 93000, "errmsg": "invalid webhook"})

    notifier = WeComNotifier(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=do-not-leak",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(WeComError) as captured:
        await notifier.send_text("hello")
    assert "do-not-leak" not in str(captured.value)
    await notifier.close()


@pytest.mark.asyncio
async def test_wecom_rejects_http_and_nonzero_http_status():
    with pytest.raises(ValueError):
        WeComNotifier("http://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x")

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="busy")

    notifier = WeComNotifier(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(WeComError, match="HTTP 503"):
        await notifier.send_text("hello")
    await notifier.close()


def test_format_notification_contains_times_delay_and_truncates_content():
    text = format_chat_notification(
        session_name="目标群",
        sender="Alice",
        content="x" * 3000,
        message_time_ms=1_000,
        received_at_ms=4_500,
    )
    assert text.startswith("【聊天室-目标群】1 条消息\n- Alice · 08:00:01:\n")
    assert "延迟" not in text
    assert len(text) <= 2048


@pytest.mark.asyncio
async def test_wecom_uses_markdown_payload_like_telegram_monitor():
    observed = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        observed["json"] = json.loads(request.content)
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    notifier = WeComNotifier(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret-key",
        transport=httpx.MockTransport(handler),
    )
    await notifier.send_text("【聊天室-目标群】1 条消息\n- Alice · 08:00:01:\nhello")
    assert observed["json"]["msgtype"] == "markdown"
    assert observed["json"]["markdown"]["content"].startswith("【聊天室-目标群】")
    await notifier.close()

