from __future__ import annotations

import ipaddress
import json
import os
import re
import tempfile
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from .protocol import (
    HEARTBEAT_SECONDS,
    MAX_MISSED_PONGS,
    MAX_RECONNECTS,
    RECONNECT_DELAY_SECONDS,
)

MAX_ENV_FILE_BYTES = 1024 * 1024
COOKIE_FILE_ENV_NAME = "BINANCE_CHAT_COOKIE_FILE"
COOKIE_FILE_SETTING_PATTERN = re.compile(rf"^\s*(?:export\s+)?{COOKIE_FILE_ENV_NAME}\s*=")


def save_cookie_file_setting(
    cookie_file: str | Path,
    *,
    env_file: str | Path = ".env",
) -> Path:
    """Atomically remember one Cookie file without storing credential values in .env."""

    cookie_path = Path(cookie_file).expanduser().resolve()
    rendered_path = str(cookie_path)
    if any(ord(char) < 32 or ord(char) == 127 for char in rendered_path):
        raise ValueError("cookie file path contains control characters")

    destination = Path(env_file).expanduser().resolve()
    existing = ""
    if destination.exists():
        if not destination.is_file():
            raise ValueError(f"environment path is not a file: {destination}")
        if destination.stat().st_size > MAX_ENV_FILE_BYTES:
            raise ValueError(f"environment file is unexpectedly large: {destination}")
        existing = destination.read_text(encoding="utf-8")

    replacement = f"{COOKIE_FILE_ENV_NAME}={json.dumps(rendered_path, ensure_ascii=False)}"
    output_lines: list[str] = []
    replaced = False
    for line in existing.splitlines():
        if COOKIE_FILE_SETTING_PATTERN.match(line):
            if not replaced:
                output_lines.append(replacement)
                replaced = True
            continue
        output_lines.append(line)
    if not replaced:
        if output_lines and output_lines[-1]:
            output_lines.append("")
        output_lines.append(replacement)
    output = "\n".join(output_lines) + "\n"

    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary_path = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(output)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
        if os.name == "posix":
            os.chmod(destination, 0o600)
            directory_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except Exception:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
        raise
    return destination


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BINANCE_CHAT_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    session_path: Path = Path("./data/binance-chat.session")
    session_encryption_key: SecretStr | None = None
    cookie_file: Path | None = None
    cookie_browser_headless: bool = True
    cookie_browser_channel: str = "chrome"
    cookie_bootstrap_timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    cookie_bootstrap_attempts: int = Field(default=2, ge=1, le=5)
    cookie_bootstrap_retry_delay_seconds: float = Field(default=2.0, ge=0, le=30)
    cookie_browser_user_agent: str | None = None
    host: str = "127.0.0.1"
    host_port: int = Field(default=8102, ge=1, le=65535)
    api_url: str | None = None
    port: int = Field(default=8102, ge=1, le=65535)
    api_key: SecretStr | None = None
    auto_connect: bool = True
    heartbeat_seconds: float = Field(default=HEARTBEAT_SECONDS, gt=0)
    max_missed_pongs: int = Field(default=MAX_MISSED_PONGS, ge=1, le=20)
    reconnect_delay_seconds: float = Field(default=RECONNECT_DELAY_SECONDS, ge=0)
    max_reconnects: int = Field(default=MAX_RECONNECTS, ge=0, le=100)
    ack_timeout_seconds: float = Field(default=10.0, gt=0)
    auth_error_codes: str = ""

    def encryption_key_value(self) -> str | None:
        if self.session_encryption_key is None:
            return None
        value = self.session_encryption_key.get_secret_value().strip()
        return value or None

    def api_key_value(self) -> str | None:
        if self.api_key is None:
            return None
        value = self.api_key.get_secret_value().strip()
        return value or None

    def parsed_auth_error_codes(self) -> frozenset[str]:
        return frozenset(code.strip() for code in self.auth_error_codes.split(",") if code.strip())

    def cookie_browser_user_agent_value(self) -> str | None:
        if self.cookie_browser_user_agent is None:
            return None
        value = self.cookie_browser_user_agent.strip()
        return value or None

    def validate_bind_security(self) -> None:
        try:
            address = ipaddress.ip_address(self.host)
            is_loopback = address.is_loopback
        except ValueError:
            is_loopback = self.host.lower() == "localhost"
        if not is_loopback and not self.api_key_value():
            raise ValueError("BINANCE_CHAT_API_KEY is required when binding outside localhost")
