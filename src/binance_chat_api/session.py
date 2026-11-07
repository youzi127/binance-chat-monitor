from __future__ import annotations

import base64
import binascii
import json
import os
import re
import shlex
import stat
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, Self
from urllib.parse import unquote, urlparse

from .exceptions import SessionError, SessionSecurityError
from .protocol import AUTH_HEADER_ALLOWLIST, BASE_URL, REQUIRED_PRIVATE_HEADERS

SCHEMA_VERSION = 1


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class CookieEntry:
    name: str
    value: str = field(repr=False)
    domain: str = ".binance.com"
    path: str = "/"
    secure: bool = True
    expires: int | None = None
    http_only: bool = False
    same_site: str | None = None
    include_subdomains: bool | None = None


COOKIE_NAME_PATTERN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
COOKIE_DOMAIN_PATTERN = re.compile(r"^\.?[A-Za-z0-9.-]+$")


def is_binance_cookie_domain(domain: str) -> bool:
    if not COOKIE_DOMAIN_PATTERN.fullmatch(domain) or ".." in domain:
        return False
    normalized = domain.lstrip(".").lower()
    return normalized == "binance.com" or normalized.endswith(".binance.com")


def validate_cookie_entry(cookie: CookieEntry) -> None:
    if not COOKIE_NAME_PATTERN.fullmatch(cookie.name):
        raise SessionSecurityError(f"invalid cookie name: {cookie.name!r}")
    if not is_binance_cookie_domain(cookie.domain):
        raise SessionSecurityError(f"refusing non-Binance cookie domain: {cookie.domain!r}")
    if not cookie.path.startswith("/") or any(
        ord(char) < 32 or ord(char) == 127 for char in cookie.path
    ):
        raise SessionSecurityError(f"invalid cookie path for {cookie.name!r}")
    if any(ord(char) < 32 or ord(char) == 127 for char in cookie.value):
        raise SessionSecurityError(f"cookie value for {cookie.name!r} contains control characters")
    if cookie.expires is not None and (
        not isinstance(cookie.expires, int)
        or isinstance(cookie.expires, bool)
        or cookie.expires < 0
    ):
        raise SessionSecurityError(f"invalid cookie expiry for {cookie.name!r}")


@dataclass(slots=True)
class BinanceSession:
    schema_version: int = SCHEMA_VERSION
    base_url: str = BASE_URL
    imported_from_host: str = "www.binance.com"
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    last_success_at: str | None = None
    uid: int | None = None
    note: str | None = None
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    cookies: list[CookieEntry] = field(default_factory=list, repr=False)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["cookies"] = [asdict(cookie) for cookie in self.cookies]
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Self:
        version = payload.get("schema_version")
        if version != SCHEMA_VERSION:
            raise SessionError(f"unsupported session schema version: {version!r}")
        cookies = [CookieEntry(**item) for item in payload.get("cookies", [])]
        headers = {
            str(key).lower(): str(value) for key, value in payload.get("headers", {}).items()
        }
        return cls(
            schema_version=version,
            base_url=str(payload.get("base_url", BASE_URL)),
            imported_from_host=str(payload.get("imported_from_host", "www.binance.com")),
            created_at=str(payload.get("created_at", _utc_now())),
            updated_at=str(payload.get("updated_at", _utc_now())),
            last_success_at=payload.get("last_success_at"),
            uid=payload.get("uid"),
            note=payload.get("note"),
            headers=headers,
            cookies=cookies,
        )

    def validate_private_chat(self, *, allow_incomplete: bool = False) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https" or parsed.hostname != "www.binance.com":
            raise SessionSecurityError("session base URL must be https://www.binance.com")
        if not self.cookies and not allow_incomplete:
            raise SessionError("the session does not contain any cookies")
        for cookie in self.cookies:
            validate_cookie_entry(cookie)

        missing = sorted(REQUIRED_PRIVATE_HEADERS.difference(self.headers))
        if missing and not allow_incomplete:
            raise SessionError("missing required browser headers: " + ", ".join(missing))

        header_csrf = self.headers.get("csrftoken")
        cookie_csrf = next(
            (cookie.value for cookie in self.cookies if cookie.name.lower() == "csrftoken"),
            None,
        )
        if header_csrf and cookie_csrf:
            if header_csrf not in {cookie_csrf, unquote(cookie_csrf)}:
                raise SessionSecurityError("csrftoken header does not match the csrftoken cookie")

    def redacted_metadata(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "base_url": self.base_url,
            "imported_from_host": self.imported_from_host,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_success_at": self.last_success_at,
            "uid": self.uid,
            "note": self.note,
            "header_names": sorted(self.headers),
            "cookie_names": sorted(cookie.name for cookie in self.cookies),
        }


class SessionPersistence(Protocol):
    def save(self, session: BinanceSession) -> None: ...


def _parse_cookie_header(raw: str) -> list[CookieEntry]:
    cookies: list[CookieEntry] = []
    for part in raw.split(";"):
        item = part.strip()
        if not item or "=" not in item:
            continue
        name, value = item.split("=", 1)
        name = name.strip()
        if not name:
            continue
        cookies.append(CookieEntry(name=name, value=value.strip()))
    return cookies


def _split_curl(curl_text: str) -> list[str]:
    normalized = curl_text.replace("\\\r\n", " ").replace("\\\n", " ").strip()
    if not normalized:
        raise SessionError("empty cURL input")
    try:
        tokens = shlex.split(normalized, posix=True)
    except ValueError as exc:
        raise SessionError(f"cannot parse cURL input: {exc}") from exc
    if not tokens or Path(tokens[0]).name != "curl":
        raise SessionError("input must start with curl")
    return tokens


def _parse_header_line(header_line: str) -> tuple[str, str]:
    if "\r" in header_line or "\n" in header_line:
        raise SessionSecurityError("cURL headers cannot contain line breaks")
    if ":" not in header_line:
        raise SessionError(f"invalid header: {header_line!r}")
    name, value = header_line.split(":", 1)
    return name.strip().lower(), value.strip()


def _enrich_user_agent(headers: dict[str, str]) -> None:
    if "user-agent" in headers:
        return
    encoded = headers.get("device-info")
    if not encoded or len(encoded) > 256 * 1024:
        return
    try:
        raw = base64.b64decode(encoded, validate=True)
        payload = json.loads(raw)
    except (binascii.Error, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict):
        return
    user_agent = payload.get("user_agent")
    if not isinstance(user_agent, str) or not 1 <= len(user_agent) <= 1024:
        return
    if any(ord(char) < 32 or ord(char) == 127 for char in user_agent):
        return
    headers["user-agent"] = user_agent


def import_curl(
    curl_text: str,
    *,
    note: str | None = None,
    allow_incomplete: bool = False,
) -> BinanceSession:
    """Parse a browser's Copy-as-cURL output without executing it.

    The original cURL text is never stored. Only an allowlisted subset of headers and
    cookies scoped to Binance are returned.
    """

    tokens = _split_curl(curl_text)
    headers: dict[str, str] = {}
    cookie_fragments: list[str] = []
    request_url: str | None = None

    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in {"-H", "--header"}:
            index += 1
            if index >= len(tokens):
                raise SessionError(f"{token} is missing its value")
            key, value = _parse_header_line(tokens[index])
            if key == "cookie":
                cookie_fragments.append(value)
            elif key in AUTH_HEADER_ALLOWLIST:
                headers[key] = value
        elif token.startswith("--header="):
            key, value = _parse_header_line(token.split("=", 1)[1])
            if key == "cookie":
                cookie_fragments.append(value)
            elif key in AUTH_HEADER_ALLOWLIST:
                headers[key] = value
        elif token in {"-b", "--cookie"}:
            index += 1
            if index >= len(tokens):
                raise SessionError(f"{token} is missing its value")
            cookie_fragments.append(tokens[index])
        elif token.startswith("--cookie="):
            cookie_fragments.append(token.split("=", 1)[1])
        elif token == "--url":
            index += 1
            if index >= len(tokens):
                raise SessionError("--url is missing its value")
            request_url = tokens[index]
        elif token.startswith("--url="):
            request_url = token.split("=", 1)[1]
        elif token.startswith("http://") or token.startswith("https://"):
            request_url = token
        index += 1

    if request_url is None:
        raise SessionError("cURL input does not contain an HTTP URL")
    parsed = urlparse(request_url)
    if parsed.scheme != "https" or parsed.hostname != "www.binance.com":
        raise SessionSecurityError("only an HTTPS request to www.binance.com can be imported")

    cookies_by_scope: dict[tuple[str, str, str], CookieEntry] = {}
    for fragment in cookie_fragments:
        for cookie in _parse_cookie_header(fragment):
            validate_cookie_entry(cookie)
            cookies_by_scope[(cookie.name, cookie.domain, cookie.path)] = cookie

    _enrich_user_agent(headers)

    session = BinanceSession(
        base_url=f"{parsed.scheme}://{parsed.hostname}",
        imported_from_host=parsed.hostname,
        note=note,
        headers=headers,
        cookies=list(cookies_by_scope.values()),
    )
    session.validate_private_chat(allow_incomplete=allow_incomplete)
    return session


def import_fetch(
    fetch_text: str,
    cookies: list[CookieEntry],
    *,
    note: str | None = None,
) -> BinanceSession:
    """Parse Chrome DevTools Copy-as-fetch text without evaluating JavaScript."""

    if len(fetch_text.encode("utf-8")) > 2 * 1024 * 1024:
        raise SessionSecurityError("Copy-as-fetch input is unexpectedly large")
    text = fetch_text.strip()
    if not text.startswith("fetch"):
        raise SessionError("input must be a Chrome DevTools Copy-as-fetch statement")

    index = len("fetch")
    while index < len(text) and text[index].isspace():
        index += 1
    if index >= len(text) or text[index] != "(":
        raise SessionError("invalid Copy-as-fetch statement")
    index += 1

    decoder = json.JSONDecoder()
    try:
        request_url, index = decoder.raw_decode(text, index)
    except json.JSONDecodeError as exc:
        raise SessionError("cannot parse the Copy-as-fetch URL") from exc
    while index < len(text) and text[index].isspace():
        index += 1
    if index >= len(text) or text[index] != ",":
        raise SessionError("Copy-as-fetch statement has no request options")
    index += 1
    while index < len(text) and text[index].isspace():
        index += 1
    try:
        options, index = decoder.raw_decode(text, index)
    except json.JSONDecodeError as exc:
        raise SessionError("cannot parse the Copy-as-fetch request options") from exc
    remainder = text[index:].strip()
    if remainder not in {")", ");"}:
        raise SessionError("unexpected trailing content in Copy-as-fetch input")

    if not isinstance(request_url, str) or not isinstance(options, dict):
        raise SessionError("invalid Copy-as-fetch input types")
    parsed = urlparse(request_url)
    if parsed.scheme != "https" or parsed.hostname != "www.binance.com":
        raise SessionSecurityError("only an HTTPS request to www.binance.com can be imported")
    raw_headers = options.get("headers")
    if not isinstance(raw_headers, dict):
        raise SessionError("Copy-as-fetch input has no headers object")

    headers: dict[str, str] = {}
    for name, value in raw_headers.items():
        key = str(name).strip().lower()
        rendered = str(value)
        if "\r" in key or "\n" in key or "\r" in rendered or "\n" in rendered:
            raise SessionSecurityError("Copy-as-fetch headers cannot contain line breaks")
        if key in AUTH_HEADER_ALLOWLIST:
            headers[key] = rendered

    _enrich_user_agent(headers)

    session = BinanceSession(
        base_url=f"{parsed.scheme}://{parsed.hostname}",
        imported_from_host=parsed.hostname,
        note=note,
        headers=headers,
        cookies=cookies,
    )
    session.validate_private_chat()
    return session


class SessionStore:
    def __init__(
        self,
        path: str | Path,
        *,
        encryption_key: str | bytes | None = None,
        strict_permissions: bool = True,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self.strict_permissions = strict_permissions
        if isinstance(encryption_key, str):
            encryption_key = encryption_key.encode()
        self.encryption_key = encryption_key
        self._write_lock = threading.RLock()

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> BinanceSession:
        if not self.path.is_file():
            raise SessionError(f"session file does not exist: {self.path}")
        mode = stat.S_IMODE(self.path.stat().st_mode)
        if self.strict_permissions and os.name == "posix" and mode & 0o077:
            raise SessionSecurityError(
                f"session file permissions must be 0600, found {mode:04o}: {self.path}"
            )
        try:
            wrapper = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SessionError(f"cannot read session file: {exc}") from exc

        if wrapper.get("format") == "fernet":
            if not self.encryption_key:
                raise SessionSecurityError(
                    "session is encrypted but BINANCE_CHAT_SESSION_ENCRYPTION_KEY is missing"
                )
            payload = self._decrypt(wrapper["ciphertext"])
        elif wrapper.get("format") == "plain-json":
            payload = wrapper.get("session")
        else:
            raise SessionError("unknown session file format")
        if not isinstance(payload, dict):
            raise SessionError("invalid session payload")
        session = BinanceSession.from_dict(payload)
        session.validate_private_chat()
        return session

    def save(self, session: BinanceSession) -> None:
        with self._write_lock:
            self._save_unlocked(session)

    def _save_unlocked(self, session: BinanceSession) -> None:
        session.validate_private_chat()
        session.updated_at = _utc_now()
        payload = session.to_dict()
        if self.encryption_key:
            wrapper = {"format": "fernet", "ciphertext": self._encrypt(payload)}
        else:
            wrapper = {"format": "plain-json", "session": payload}

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
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(wrapper, handle, ensure_ascii=False, separators=(",", ":"))
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

    def _encrypt(self, payload: dict[str, Any]) -> str:
        try:
            from cryptography.fernet import Fernet
        except ImportError as exc:
            raise SessionSecurityError(
                "encrypted sessions require: pip install 'binance-chat-api[crypto]'"
            ) from exc
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        return Fernet(self.encryption_key).encrypt(raw).decode()

    def _decrypt(self, ciphertext: str) -> dict[str, Any]:
        try:
            from cryptography.fernet import Fernet, InvalidToken
        except ImportError as exc:
            raise SessionSecurityError(
                "encrypted sessions require: pip install 'binance-chat-api[crypto]'"
            ) from exc
        try:
            raw = Fernet(self.encryption_key).decrypt(ciphertext.encode())
            return json.loads(raw)
        except (InvalidToken, json.JSONDecodeError) as exc:
            raise SessionSecurityError("cannot decrypt session file") from exc


class SessionFileLock:
    """Best-effort single-process guard for one Binance browser session."""

    def __init__(self, session_path: str | Path) -> None:
        self.path = Path(str(session_path) + ".lock")
        self._handle: Any = None

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._handle = self.path.open("a+", encoding="utf-8")
        if os.name == "posix":
            import fcntl

            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                self._handle.close()
                self._handle = None
                raise SessionSecurityError(
                    "another process is already using this Binance session"
                ) from exc
            os.chmod(self.path, 0o600)
        elif os.name == "nt":
            import msvcrt

            self._handle.seek(0)
            self._handle.write("0")
            self._handle.flush()
            self._handle.seek(0)
            try:
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                self._handle.close()
                self._handle = None
                raise SessionSecurityError(
                    "another process is already using this Binance session"
                ) from exc
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._handle is None:
            return
        if os.name == "posix":
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        elif os.name == "nt":
            import msvcrt

            self._handle.seek(0)
            try:
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        self._handle.close()
        self._handle = None


def generate_fernet_key() -> str:
    """Generate a Fernet-compatible key without importing optional crypto."""

    return base64.urlsafe_b64encode(os.urandom(32)).decode()
