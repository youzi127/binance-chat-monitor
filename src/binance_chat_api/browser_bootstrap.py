from __future__ import annotations

import asyncio
import platform
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .client import BinanceChatClient
from .config import Settings
from .cookie_file import NetscapeCookieStore, from_playwright_cookies, to_playwright_cookies
from .exceptions import (
    BinanceChatError,
    BrowserBootstrapRetryableError,
    BrowserBootstrapTimeoutError,
    BrowserBootstrapTransientError,
    SessionError,
    SessionExpiredError,
)
from .protocol import (
    AUTH_HEADER_ALLOWLIST,
    CHAT_REFERER,
    GET_MY_PROFILE,
    REQUIRED_PRIVATE_HEADERS,
    SUCCESS_CODE,
)
from .session import BinanceSession, SessionFileLock


def _browser_user_agent(version: str) -> str:
    if platform.system() == "Darwin":
        system = "Macintosh; Intel Mac OS X 10_15_7"
    elif platform.system() == "Windows":
        system = "Windows NT 10.0; Win64; x64"
    else:
        system = "X11; Linux x86_64"
    return (
        f"Mozilla/5.0 ({system}) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{version} Safari/537.36"
    )


def _playwright_channel(channel: str) -> str | None:
    normalized = channel.strip()
    if not normalized or normalized.casefold() in {"bundled", "playwright"}:
        return None
    return normalized


def _filter_auth_headers(headers: dict[str, str]) -> dict[str, str]:
    filtered: dict[str, str] = {}
    for name, value in headers.items():
        key = name.lower()
        if key not in AUTH_HEADER_ALLOWLIST:
            continue
        if "\r" in value or "\n" in value:
            raise SessionError(f"temporary browser returned an invalid {key!r} header")
        filtered[key] = value
    missing = sorted(REQUIRED_PRIVATE_HEADERS.difference(filtered))
    if missing:
        raise SessionError(
            "temporary browser request is missing required headers: " + ", ".join(missing)
        )
    return filtered


async def _bootstrap_cookie_session_once(
    store: NetscapeCookieStore,
    *,
    headless: bool = True,
    channel: str = "chrome",
    timeout_seconds: float = 60.0,
    user_agent: str | None = None,
) -> BinanceSession:
    """Open Binance briefly, capture generated headers, sync cookies, then close Chrome."""

    try:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise SessionError(
            "cookie-file bootstrap requires: pip install 'binance-chat-api[browser]'"
        ) from exc

    source_cookies = store.load_cookies()
    capture_tasks: set[asyncio.Task[None]] = set()
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                channel=_playwright_channel(channel),
                headless=headless,
            )
            try:
                effective_user_agent = user_agent or _browser_user_agent(browser.version)
                context = await browser.new_context(
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai",
                    user_agent=effective_user_agent,
                    viewport={"width": 1280, "height": 800},
                )
                await context.add_cookies(to_playwright_cookies(source_cookies))
                page = await context.new_page()
                loop = asyncio.get_running_loop()
                captured: asyncio.Future[tuple[Any, dict[str, str]]] = loop.create_future()

                async def inspect_request(request: Any) -> None:
                    if urlparse(request.url).path != GET_MY_PROFILE:
                        return
                    headers = _filter_auth_headers(await request.all_headers())
                    if not captured.done():
                        captured.set_result((request, headers))

                def on_request(request: Any) -> None:
                    task = asyncio.create_task(inspect_request(request))
                    capture_tasks.add(task)

                    def finish(completed: asyncio.Task[None]) -> None:
                        capture_tasks.discard(completed)
                        if completed.cancelled() or captured.done():
                            return
                        error = completed.exception()
                        if error is not None:
                            captured.set_exception(error)

                    task.add_done_callback(finish)

                page.on("request", on_request)
                try:
                    await page.goto(
                        CHAT_REFERER,
                        wait_until="domcontentloaded",
                        timeout=timeout_seconds * 1000,
                    )
                except PlaywrightTimeoutError as exc:
                    raise BrowserBootstrapTimeoutError("navigation", timeout_seconds) from exc
                try:
                    request, headers = await asyncio.wait_for(captured, timeout=timeout_seconds)
                except TimeoutError as exc:
                    raise BrowserBootstrapTimeoutError(
                        "authenticated-request capture", timeout_seconds
                    ) from exc
                try:
                    response = await asyncio.wait_for(request.response(), timeout=15)
                except TimeoutError as exc:
                    raise BrowserBootstrapTimeoutError("profile response", 15) from exc
                if response is None:
                    raise SessionError("temporary browser did not receive a profile response")
                try:
                    payload = await response.json()
                except Exception as exc:
                    raise SessionError(
                        "temporary browser received a non-JSON profile response"
                    ) from exc
                code = str(payload.get("code", "")) if isinstance(payload, dict) else ""
                if (
                    not isinstance(payload, dict)
                    or not payload.get("success", False)
                    or code != SUCCESS_CODE
                    or not isinstance(payload.get("data"), dict)
                ):
                    raise SessionExpiredError(
                        "cookie file was rejected by the Binance profile endpoint "
                        f"(code={code or 'unknown'})"
                    )

                browser_cookies = await context.cookies(["https://www.binance.com"])
                session = BinanceSession(
                    note="bootstrapped from Netscape cookie file",
                    headers=headers,
                    cookies=from_playwright_cookies(browser_cookies),
                )
                session.validate_private_chat()
                store.save(session)
                return session
            finally:
                pending_capture_tasks = tuple(capture_tasks)
                for task in pending_capture_tasks:
                    task.cancel()
                for task in pending_capture_tasks:
                    with suppress(asyncio.CancelledError, Exception):
                        await task
                with suppress(PlaywrightError):
                    await browser.close()
    except SessionError:
        raise
    except (TimeoutError, PlaywrightTimeoutError) as exc:
        raise BrowserBootstrapTimeoutError("browser bootstrap", timeout_seconds) from exc
    except PlaywrightError as exc:
        detail = str(exc)[:300]
        normalized = detail.lower()
        if "executable doesn't exist" in normalized or "distribution 'chrome'" in normalized:
            raise SessionError(
                "installed Google Chrome was not found; install Chrome or configure "
                "BINANCE_CHAT_COOKIE_BROWSER_CHANNEL"
            ) from exc
        raise BrowserBootstrapTransientError(
            f"temporary Chrome bootstrap failed: {detail}"
        ) from exc


async def bootstrap_cookie_session(
    store: NetscapeCookieStore,
    *,
    headless: bool = True,
    channel: str = "chrome",
    timeout_seconds: float = 60.0,
    user_agent: str | None = None,
    attempts: int = 2,
    retry_delay_seconds: float = 2.0,
) -> BinanceSession:
    """Bootstrap a Cookie-backed session, retrying only transient browser failures."""

    if not 1 <= attempts <= 5:
        raise ValueError("attempts must be between 1 and 5")
    if retry_delay_seconds < 0:
        raise ValueError("retry_delay_seconds cannot be negative")

    for attempt in range(1, attempts + 1):
        try:
            return await _bootstrap_cookie_session_once(
                store,
                headless=headless,
                channel=channel,
                timeout_seconds=timeout_seconds,
                user_agent=user_agent,
            )
        except BrowserBootstrapRetryableError as exc:
            if attempt >= attempts:
                if attempts == 1:
                    raise
                raise BrowserBootstrapTransientError(
                    f"temporary Chrome bootstrap failed after {attempts} attempts; "
                    f"last error: {exc}"
                ) from exc
            if retry_delay_seconds:
                await asyncio.sleep(retry_delay_seconds)

    raise AssertionError("unreachable browser bootstrap state")


async def prepare_cookie_session(
    store: NetscapeCookieStore,
    *,
    force_browser: bool = False,
    headless: bool = True,
    channel: str = "chrome",
    timeout_seconds: float = 60.0,
    user_agent: str | None = None,
    attempts: int = 2,
    retry_delay_seconds: float = 2.0,
) -> BinanceSession:
    """Use embedded headers when valid, otherwise refresh them with temporary Chrome."""

    if not force_browser:
        try:
            session = store.load_session()
        except SessionError:
            session = None
        if session is not None:
            probe = BinanceChatClient(session)
            try:
                await probe.get_my_profile()
            except BinanceChatError:
                pass
            else:
                store.save(session)
                return session
            finally:
                await probe.stop()

    return await bootstrap_cookie_session(
        store,
        headless=headless,
        channel=channel,
        timeout_seconds=timeout_seconds,
        user_agent=user_agent,
        attempts=attempts,
        retry_delay_seconds=retry_delay_seconds,
    )


@asynccontextmanager
async def cookie_client(
    path: str | Path,
    *,
    headless: bool = True,
    channel: str = "chrome",
    timeout_seconds: float = 60.0,
    user_agent: str | None = None,
    bootstrap_attempts: int = 2,
    bootstrap_retry_delay_seconds: float = 2.0,
    **client_options: Any,
) -> AsyncIterator[BinanceChatClient]:
    """Open a locked, Cookie-file-backed BinanceChatClient."""

    store = NetscapeCookieStore(path)
    with SessionFileLock(store.path):
        session = await prepare_cookie_session(
            store,
            headless=headless,
            channel=channel,
            timeout_seconds=timeout_seconds,
            user_agent=user_agent,
            attempts=bootstrap_attempts,
            retry_delay_seconds=bootstrap_retry_delay_seconds,
        )
        client = BinanceChatClient(session, session_store=store, **client_options)
        async with client:
            yield client


@asynccontextmanager
async def configured_cookie_client(
    settings: Settings | None = None,
    **client_options: Any,
) -> AsyncIterator[BinanceChatClient]:
    """Open the Cookie session remembered by ``binance-chat setup``."""

    settings = settings or Settings()
    if settings.cookie_file is None:
        raise SessionError(
            "no Cookie file is configured; run 'binance-chat setup /path/to/cookies.txt'"
        )
    async with cookie_client(
        settings.cookie_file,
        headless=settings.cookie_browser_headless,
        channel=settings.cookie_browser_channel,
        timeout_seconds=settings.cookie_bootstrap_timeout_seconds,
        user_agent=settings.cookie_browser_user_agent_value(),
        bootstrap_attempts=settings.cookie_bootstrap_attempts,
        bootstrap_retry_delay_seconds=settings.cookie_bootstrap_retry_delay_seconds,
        **client_options,
    ) as client:
        yield client
