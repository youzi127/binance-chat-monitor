from __future__ import annotations

import base64
import json
import os
import stat
import tempfile
import threading
from pathlib import Path
from time import time
from typing import Any

from .exceptions import SessionError, SessionSecurityError
from .protocol import AUTH_HEADER_ALLOWLIST
from .session import (
    BinanceSession,
    CookieEntry,
    is_binance_cookie_domain,
    validate_cookie_entry,
)

NETSCAPE_HEADER = """# Netscape HTTP Cookie File
# Managed by binance-chat-api. Cookie values are credentials; keep this file private.
"""
MAX_COOKIE_FILE_BYTES = 10 * 1024 * 1024
SESSION_COMMENT_PREFIX = "# binance-chat-api-session-v1 "


class NetscapeCookieStore:
    """Read and atomically update a curl/Netscape cookie-jar file."""

    def __init__(
        self,
        path: str | Path,
        *,
        strict_permissions: bool = True,
        repair_permissions: bool = True,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self.strict_permissions = strict_permissions
        self.repair_permissions = repair_permissions
        self._write_lock = threading.RLock()

    def exists(self) -> bool:
        return self.path.is_file()

    def load_cookies(self) -> list[CookieEntry]:
        cookies, _ = self._load()
        return cookies

    def load_session(self, *, allow_incomplete: bool = False) -> BinanceSession:
        cookies, metadata = self._load()
        metadata = metadata or {}
        raw_headers = metadata.get("headers") or {}
        if not isinstance(raw_headers, dict):
            raise SessionSecurityError("invalid embedded Binance Chat header metadata")
        headers: dict[str, str] = {}
        for name, value in raw_headers.items():
            key = str(name).strip().lower()
            rendered = str(value)
            if any(char in key or char in rendered for char in "\r\n"):
                raise SessionSecurityError("embedded Binance Chat headers contain line breaks")
            if key in AUTH_HEADER_ALLOWLIST:
                headers[key] = rendered
        uid = metadata.get("uid")
        if uid is not None and (not isinstance(uid, int) or isinstance(uid, bool)):
            raise SessionSecurityError("invalid embedded Binance Chat uid metadata")
        now = int(time())
        active_cookies = [
            cookie for cookie in cookies if cookie.expires is None or cookie.expires > now
        ]
        session = BinanceSession(
            uid=uid,
            note="Netscape cookie file",
            headers=headers,
            cookies=active_cookies,
        )
        session.validate_private_chat(allow_incomplete=allow_incomplete)
        return session

    def _load(self) -> tuple[list[CookieEntry], dict[str, Any] | None]:
        if not self.path.is_file():
            raise SessionError(f"cookie file does not exist: {self.path}")
        size = self.path.stat().st_size
        if size > MAX_COOKIE_FILE_BYTES:
            raise SessionSecurityError(
                f"cookie file is unexpectedly large ({size} bytes): {self.path}"
            )
        self._secure_permissions()
        try:
            text = self.path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            raise SessionError(f"cannot read Netscape cookie file: {exc}") from exc
        return _parse_netscape_cookie_text(text, source=str(self.path))

    def save(self, session: BinanceSession) -> None:
        session.validate_private_chat()
        metadata = {
            "headers": {
                name: value
                for name, value in session.headers.items()
                if name in AUTH_HEADER_ALLOWLIST
            },
            "uid": session.uid,
        }
        self._save_cookies(session.cookies, metadata=metadata)

    def save_cookies(self, cookies: list[CookieEntry]) -> None:
        self._save_cookies(cookies, metadata=None)

    def _save_cookies(
        self,
        cookies: list[CookieEntry],
        *,
        metadata: dict[str, Any] | None,
    ) -> None:
        if not cookies:
            raise SessionError("refusing to replace the cookie file with an empty jar")
        with self._write_lock:
            lines = [NETSCAPE_HEADER.rstrip("\n")]
            if metadata is not None:
                lines.append(SESSION_COMMENT_PREFIX + _encode_session_metadata(metadata))
            for cookie in sorted(
                cookies,
                key=lambda item: (item.domain.lower(), item.path, item.name.lower()),
            ):
                validate_cookie_entry(cookie)
                domain = cookie.domain
                if cookie.http_only:
                    domain = "#HttpOnly_" + domain
                include_subdomains = cookie.include_subdomains
                if include_subdomains is None:
                    include_subdomains = cookie.domain.startswith(".")
                fields = (
                    domain,
                    "TRUE" if include_subdomains else "FALSE",
                    cookie.path,
                    "TRUE" if cookie.secure else "FALSE",
                    str(cookie.expires or 0),
                    cookie.name,
                    cookie.value,
                )
                lines.append("\t".join(fields))
            self._atomic_write("\n".join(lines) + "\n")

    def _secure_permissions(self) -> None:
        if not self.strict_permissions or os.name != "posix":
            return
        mode = stat.S_IMODE(self.path.stat().st_mode)
        if not mode & 0o077:
            return
        if not self.repair_permissions:
            raise SessionSecurityError(
                f"cookie file permissions must be 0600, found {mode:04o}: {self.path}"
            )
        try:
            os.chmod(self.path, 0o600)
        except OSError as exc:
            raise SessionSecurityError(
                f"cannot restrict cookie file permissions to 0600: {self.path}"
            ) from exc

    def _atomic_write(self, text: str) -> None:
        parent_existed = self.path.parent.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix" and not parent_existed:
            os.chmod(self.path.parent, 0o700)
        fd, temporary_path = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        try:
            if os.name == "posix":
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
            if os.name == "posix":
                os.chmod(self.path, 0o600)
                directory_fd = os.open(self.path.parent, os.O_RDONLY)
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


def parse_netscape_cookie_text(text: str) -> list[CookieEntry]:
    """Validate an uploaded Netscape cookie jar without writing credential data."""

    size = len(text.encode("utf-8"))
    if size > MAX_COOKIE_FILE_BYTES:
        raise SessionSecurityError(f"cookie upload is unexpectedly large ({size} bytes)")
    cookies, _ = _parse_netscape_cookie_text(text, source="uploaded cookie file")
    return cookies


def _parse_netscape_cookie_text(
    text: str,
    *,
    source: str,
) -> tuple[list[CookieEntry], dict[str, Any] | None]:
    cookies: list[CookieEntry] = []
    metadata: dict[str, Any] | None = None
    for line_number, source_line in enumerate(text.splitlines(), start=1):
        line = source_line
        http_only = False
        if line.startswith(SESSION_COMMENT_PREFIX):
            if metadata is not None:
                raise SessionSecurityError("cookie file contains duplicate Binance Chat metadata")
            metadata = _decode_session_metadata(
                line[len(SESSION_COMMENT_PREFIX) :], line_number=line_number
            )
            continue
        if line.startswith("#HttpOnly_"):
            http_only = True
            line = line[len("#HttpOnly_") :]
        elif not line or line.startswith("#"):
            continue

        fields = line.split("\t", 6)
        if len(fields) != 7:
            raise SessionError(
                f"invalid Netscape cookie row at line {line_number}: expected 7 fields"
            )
        domain, include_subdomains, path, secure, expires, name, value = fields
        if include_subdomains not in {"TRUE", "FALSE"}:
            raise SessionError(f"invalid include-subdomains flag at cookie line {line_number}")
        if secure not in {"TRUE", "FALSE"}:
            raise SessionError(f"invalid secure flag at cookie line {line_number}")
        try:
            raw_expiry = int(expires)
        except ValueError as exc:
            raise SessionError(f"invalid expiry at cookie line {line_number}") from exc

        cookie = CookieEntry(
            name=name,
            value=value,
            domain=domain,
            path=path or "/",
            secure=secure == "TRUE",
            expires=raw_expiry or None,
            http_only=http_only,
            include_subdomains=include_subdomains == "TRUE",
        )
        validate_cookie_entry(cookie)
        cookies.append(cookie)

    if not cookies:
        raise SessionError(f"Netscape cookie file contains no cookies: {source}")
    return cookies, metadata


def to_playwright_cookies(cookies: list[CookieEntry]) -> list[dict[str, Any]]:
    now = int(time())
    result: list[dict[str, Any]] = []
    for cookie in cookies:
        validate_cookie_entry(cookie)
        if cookie.expires is not None and cookie.expires <= now:
            continue
        item: dict[str, Any] = {
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain,
            "path": cookie.path,
            "secure": cookie.secure,
            "httpOnly": cookie.http_only,
        }
        if cookie.expires is not None:
            item["expires"] = float(cookie.expires)
        if cookie.same_site in {"Strict", "Lax", "None"}:
            item["sameSite"] = cookie.same_site
        result.append(item)
    if not result:
        raise SessionError("cookie file has no unexpired Binance cookies")
    return result


def from_playwright_cookies(cookies: list[dict[str, Any]]) -> list[CookieEntry]:
    result: list[CookieEntry] = []
    for item in cookies:
        domain = str(item.get("domain") or "")
        if not is_binance_cookie_domain(domain):
            continue
        raw_expiry = item.get("expires")
        expires = (
            int(raw_expiry) if isinstance(raw_expiry, (int, float)) and raw_expiry > 0 else None
        )
        same_site = item.get("sameSite")
        cookie = CookieEntry(
            name=str(item.get("name") or ""),
            value=str(item.get("value") or ""),
            domain=domain,
            path=str(item.get("path") or "/"),
            secure=bool(item.get("secure")),
            expires=expires,
            http_only=bool(item.get("httpOnly")),
            same_site=str(same_site) if same_site in {"Strict", "Lax", "None"} else None,
            include_subdomains=domain.startswith("."),
        )
        validate_cookie_entry(cookie)
        result.append(cookie)
    if not result:
        raise SessionError("temporary browser returned no Binance cookies")
    return result


def _encode_session_metadata(metadata: dict[str, Any]) -> str:
    raw = json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_session_metadata(encoded: str, *, line_number: int) -> dict[str, Any]:
    if len(encoded) > 512 * 1024:
        raise SessionSecurityError("embedded Binance Chat metadata is unexpectedly large")
    padding = "=" * (-len(encoded) % 4)
    try:
        raw = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
        payload = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise SessionError(
            f"invalid embedded Binance Chat metadata at cookie line {line_number}"
        ) from exc
    if not isinstance(payload, dict):
        raise SessionError(f"invalid embedded Binance Chat metadata at cookie line {line_number}")
    return payload
