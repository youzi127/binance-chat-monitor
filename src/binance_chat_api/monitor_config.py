from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, Self
from urllib.parse import parse_qs, urlparse

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class MonitorSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BINANCE_CHAT_",
        env_file=".env.monitor",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    cookie_file: Path
    target_session_id: str | None = Field(default=None, min_length=1)
    target_session_name: str | None = Field(default=None, min_length=1)
    target_session_type: Literal["GROUP"] = "GROUP"
    wecom_webhook_url: SecretStr = Field(
        validation_alias=AliasChoices(
            "WECOM_WEBHOOK_URL",
            "BINANCE_CHAT_WECOM_WEBHOOK_URL",
        )
    )
    monitor_db: Path = Path("./data/monitor.sqlite3")
    monitor_log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @classmethod
    def load(cls, env_file: str | Path = ".env.monitor") -> Self:
        return cls(_env_file=Path(env_file).expanduser())

    @field_validator("cookie_file")
    @classmethod
    def cookie_must_exist(cls, value: Path) -> Path:
        resolved = value.expanduser().resolve()
        if not resolved.is_file():
            raise ValueError("BINANCE_CHAT_COOKIE_FILE must reference a readable file")
        return resolved

    @field_validator("target_session_id", "target_session_name", mode="before")
    @classmethod
    def empty_target_is_missing(cls, value: str | None) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    @field_validator("monitor_db")
    @classmethod
    def resolve_database_path(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @field_validator("wecom_webhook_url")
    @classmethod
    def validate_wecom_url(cls, value: SecretStr) -> SecretStr:
        parsed = urlparse(value.get_secret_value())
        keys = parse_qs(parsed.query).get("key", [])
        if parsed.scheme != "https" or parsed.hostname != "qyapi.weixin.qq.com":
            raise ValueError("WECOM_WEBHOOK_URL must use HTTPS on qyapi.weixin.qq.com")
        if parsed.path != "/cgi-bin/webhook/send" or not keys or not keys[0].strip():
            raise ValueError("WECOM_WEBHOOK_URL must be a WeCom robot webhook")
        return value

    def wecom_webhook_value(self) -> str:
        return self.wecom_webhook_url.get_secret_value()


def autofill_target_session(env_file: str | Path, *, chats: list[dict]) -> MonitorSettings:
    path = Path(env_file).expanduser().resolve()
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    cookie_value = values.get("BINANCE_CHAT_COOKIE_FILE", "")
    if cookie_value and not Path(cookie_value).expanduser().is_absolute():
        cookie_path = (path.parent / cookie_value).resolve()
        text = re.sub(
            r"^\s*BINANCE_CHAT_COOKIE_FILE\s*=.*$",
            lambda _: f"BINANCE_CHAT_COOKIE_FILE={cookie_path}",
            text,
            flags=re.MULTILINE,
        )
    groups = [chat for chat in chats if str(chat.get("sessionType", "")).upper() == "GROUP"]
    session_id = values.get("BINANCE_CHAT_TARGET_SESSION_ID", "").strip()
    session_name = values.get("BINANCE_CHAT_TARGET_SESSION_NAME", "").strip()
    if session_id:
        matches = [chat for chat in groups if str(chat.get("sessionId")) == session_id]
        if not matches:
            raise ValueError(f"target session id not found in accessible groups: {session_id}")
        discovered_name = str(matches[0].get("sessionName") or "").strip()
        if discovered_name and discovered_name != session_name:
            session_name = discovered_name
    elif session_name:
        matches = [
            chat for chat in groups if str(chat.get("sessionName") or "").strip() == session_name
        ]
        if len(matches) != 1:
            raise ValueError(
                f"target session name must match exactly one accessible group: {session_name}"
            )
        session_id = str(matches[0]["sessionId"])
    else:
        raise ValueError("set BINANCE_CHAT_TARGET_SESSION_ID or BINANCE_CHAT_TARGET_SESSION_NAME")
    replacements = {
        "BINANCE_CHAT_TARGET_SESSION_ID": session_id,
        "BINANCE_CHAT_TARGET_SESSION_NAME": session_name,
    }
    for key, value in replacements.items():
        pattern = re.compile(rf"^\s*{re.escape(key)}\s*=.*$", re.MULTILINE)
        line = f"{key}={value}"
        if pattern.search(text):
            text = pattern.sub(line, text)
        else:
            text = text.rstrip("\n") + "\n" + line + "\n"
    path.write_text(text, encoding="utf-8")
    return MonitorSettings.load(path)

