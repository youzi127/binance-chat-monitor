from __future__ import annotations

from binance_chat_api.session import BinanceSession, CookieEntry


def make_session() -> BinanceSession:
    return BinanceSession(
        headers={
            "bnc-uuid": "dummy-bnc-uuid",
            "clienttype": "web",
            "csrftoken": "dummy-csrf",
            "device-info": "dummy-device-info",
            "fvideo-id": "f" * 40,
            "fvideo-token": "dummy-fvideo-token",
            "lang": "zh-CN",
            "user-agent": "pytest",
            "x-passthrough-token": "dummy-pass-token",
        },
        cookies=[
            CookieEntry(name="csrftoken", value="dummy-csrf"),
            CookieEntry(name="session", value="dummy-session"),
        ],
    )
