from pathlib import Path

import pytest
from pydantic import ValidationError

from binance_chat_api.monitor_config import MonitorSettings


def valid_cookie(tmp_path: Path) -> Path:
    cookie = tmp_path / "www.binance.com_cookies.txt"
    cookie.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    return cookie


def test_monitor_settings_accepts_one_group_and_https_wecom(tmp_path: Path):
    settings = MonitorSettings(
        cookie_file=valid_cookie(tmp_path),
        target_session_id="group-123",
        target_session_type="GROUP",
        wecom_webhook_url="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret",
        monitor_db=tmp_path / "monitor.sqlite3",
    )

    assert settings.target_session_id == "group-123"
    assert settings.wecom_webhook_value().startswith("https://qyapi.weixin.qq.com/")


@pytest.mark.parametrize(
    "url",
    [
        "http://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x",
        "https://evil.example/cgi-bin/webhook/send?key=x",
        "https://qyapi.weixin.qq.com.evil.example/cgi-bin/webhook/send?key=x",
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send",
    ],
)
def test_monitor_settings_rejects_unsafe_wecom_url(tmp_path: Path, url: str):
    with pytest.raises(ValidationError):
        MonitorSettings(
            cookie_file=valid_cookie(tmp_path),
            target_session_id="group-123",
            target_session_type="GROUP",
            wecom_webhook_url=url,
            monitor_db=tmp_path / "monitor.sqlite3",
        )


def test_monitor_settings_rejects_private_session(tmp_path: Path):
    with pytest.raises(ValidationError):
        MonitorSettings(
            cookie_file=valid_cookie(tmp_path),
            target_session_id="private-123",
            target_session_type="PRIVATE",
            wecom_webhook_url="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x",
            monitor_db=tmp_path / "monitor.sqlite3",
        )


def test_monitor_settings_rejects_missing_cookie_file(tmp_path: Path):
    with pytest.raises(ValidationError):
        MonitorSettings(
            cookie_file=tmp_path / "missing.txt",
            target_session_id="group-123",
            target_session_type="GROUP",
            wecom_webhook_url="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x",
            monitor_db=tmp_path / "monitor.sqlite3",
        )

