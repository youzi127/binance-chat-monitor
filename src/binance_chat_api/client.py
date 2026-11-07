from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from time import monotonic, time
from typing import Any
from urllib.parse import urlencode

import httpx
from websockets.asyncio.client import connect

from .events import EventHub
from .exceptions import (
    ApiError,
    MessageAckTimeout,
    NotConnectedError,
    ProtocolError,
    RateLimitedError,
    SessionExpiredError,
    TargetNotFoundError,
)
from .models import (
    ChatListResponse,
    ClientStatus,
    MessageListResponse,
    SentMessage,
    UnreadChat,
    UnreadListResponse,
)
from .protocol import (
    BASE_URL,
    CHAT_LIST,
    CHAT_REFERER,
    GET_MY_PROFILE,
    GET_WS_TOKEN,
    MARK_MESSAGE_READ,
    MAX_MISSED_PONGS,
    MAX_RECONNECTS,
    MESSAGE_LIST,
    RECONNECT_DELAY_SECONDS,
    SUCCESS_CODE,
    WS_ORIGIN,
    WS_URL,
    ClientState,
    MessageType,
    SessionType,
)
from .session import (
    BinanceSession,
    CookieEntry,
    SessionPersistence,
    is_binance_cookie_domain,
    validate_cookie_entry,
)

logger = logging.getLogger(__name__)
EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(slots=True)
class PendingConfirmation:
    future: asyncio.Future[dict[str, Any]]
    session_id: str
    session_type: SessionType


class BinanceChatClient:
    """Telethon-style lifecycle around Binance Square's private web chat protocol.

    This class does not log in. It reuses a browser session explicitly imported by the
    account owner, obtains a short-lived WebSocket token at runtime, and never persists
    that short-lived token.
    """

    def __init__(
        self,
        session: BinanceSession,
        *,
        session_store: SessionPersistence | None = None,
        heartbeat_seconds: float = 15.0,
        max_missed_pongs: int = MAX_MISSED_PONGS,
        reconnect_delay_seconds: float = RECONNECT_DELAY_SECONDS,
        max_reconnects: int = MAX_RECONNECTS,
        ack_timeout_seconds: float = 10.0,
        auth_error_codes: set[str] | frozenset[str] = frozenset(),
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        session.validate_private_chat()
        self.session = session
        self.session_store = session_store
        self.heartbeat_seconds = heartbeat_seconds
        self.max_missed_pongs = max_missed_pongs
        self.reconnect_delay_seconds = reconnect_delay_seconds
        self.max_reconnects = max_reconnects
        self.ack_timeout_seconds = ack_timeout_seconds
        self.stable_connection_seconds = max(30.0, heartbeat_seconds * 2)
        self.auth_error_codes = frozenset(auth_error_codes)
        self.http_transport = http_transport

        self.events = EventHub()
        self.state = ClientState.DISCONNECTED
        self.reconnect_attempts = 0
        self.last_error: str | None = None

        self._http: httpx.AsyncClient | None = None
        self._ws: Any = None
        self._runner_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._connected_event = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._pending: dict[str, PendingConfirmation] = {}
        self._local_id_fingerprints: dict[str, tuple[tuple[str, str, str], float]] = {}
        self._event_handlers: list[EventHandler] = []
        self._awaiting_pong = False
        self._missed_pongs = 0

    async def __aenter__(self) -> BinanceChatClient:
        await self.start()
        await self.wait_until_connected(timeout=20)
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()

    @property
    def connected(self) -> bool:
        return self.state == ClientState.CONNECTED and self._ws is not None

    def add_event_handler(self, handler: EventHandler) -> None:
        self._event_handlers.append(handler)

    async def start(self) -> None:
        if self._runner_task and not self._runner_task.done():
            return
        self._stop_event.clear()
        self._connected_event.clear()
        self.last_error = None
        self.state = ClientState.CONNECTING
        await self._ensure_http()
        self._runner_task = asyncio.create_task(self._run_forever(), name="binance-chat-connection")

    async def stop(self) -> None:
        self._stop_event.set()
        self._connected_event.clear()
        if self._runner_task:
            self._runner_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._runner_task
            self._runner_task = None
        await self._close_ws()
        self._fail_pending(NotConnectedError("client stopped"))
        if self._http:
            await self._http.aclose()
            self._http = None
        if self.session_store:
            self.session_store.save(self.session)
        self.state = ClientState.STOPPED

    async def restart(self) -> None:
        await self.stop()
        self.state = ClientState.DISCONNECTED
        await self.start()

    async def wait_until_connected(self, *, timeout: float | None = None) -> None:
        if self.connected:
            return
        waiter = asyncio.create_task(self._connected_event.wait())
        tasks: set[asyncio.Task[Any]] = {waiter}
        if self._runner_task is not None:
            tasks.add(self._runner_task)
        try:
            done, _ = await asyncio.wait(
                tasks,
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if waiter in done and waiter.result():
                return
            if self.state == ClientState.SESSION_EXPIRED:
                raise SessionExpiredError(self.last_error or "session expired")
            if self.state == ClientState.ERROR:
                raise ProtocolError(self.last_error or "connection failed")
            raise NotConnectedError("timed out waiting for Binance Chat")
        finally:
            if not waiter.done():
                waiter.cancel()
                with suppress(asyncio.CancelledError):
                    await waiter

    async def get_my_profile(self) -> dict[str, Any]:
        payload = await self._request("GET", GET_MY_PROFILE)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise SessionExpiredError("get-my-profile returned no private profile")
        return data

    async def get_ws_token(self) -> tuple[int, str]:
        payload = await self._request("POST", GET_WS_TOKEN, json_body={})
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ProtocolError("common/token returned no data")
        uid = data.get("uid")
        token = data.get("token")
        if not isinstance(uid, int) or not isinstance(token, str) or not token:
            raise ProtocolError("common/token returned an invalid uid or token")
        if self.session.uid is not None and self.session.uid != uid:
            raise SessionExpiredError(
                "common/token returned a different uid; "
                "the imported browser session may have expired"
            )
        self.session.uid = uid
        return uid, token

    async def list_chats(
        self,
        *,
        rows: int = 20,
        include_archived: bool = False,
        session_list_type: int = 0,
        scenario: int = 1,
        entry: str = "Homepage",
    ) -> ChatListResponse:
        if not 1 <= rows <= 100:
            raise ValueError("rows must be between 1 and 100")
        payload = await self._request(
            "POST",
            CHAT_LIST,
            json_body={
                "rows": rows,
                "direction": "DOWN",
                "isArchived": include_archived,
                "sessionListType": session_list_type,
                "scenario": scenario,
                "entry": entry,
            },
        )
        chats = payload.get("data") or []
        if not isinstance(chats, list):
            raise ProtocolError("chat-list data is not a list")
        total = payload.get("total", len(chats))
        return ChatListResponse(chats=chats, total=int(total))

    async def resolve_target(self, target: str) -> tuple[str, SessionType]:
        response = await self.list_chats(rows=100)
        wanted = target.strip().casefold()
        matches: list[dict[str, Any]] = []
        for chat in response.chats:
            detail = chat.get("userDetailVo") or {}
            candidates = {
                chat.get("sessionId"),
                chat.get("sessionName"),
                detail.get("nickName"),
                detail.get("chatId"),
                detail.get("userId"),
            }
            if any(isinstance(value, str) and value.casefold() == wanted for value in candidates):
                matches.append(chat)
        if len(matches) != 1:
            raise TargetNotFoundError(
                f"target {target!r} matched {len(matches)} chats; use an exact session_id"
            )
        match = matches[0]
        session_id = match.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise ProtocolError("matched chat has no sessionId")
        raw_type = str(match.get("sessionType") or SessionType.PRIVATE.value).upper()
        session_type = SessionType.GROUP if raw_type == SessionType.GROUP else SessionType.PRIVATE
        return session_id, session_type

    async def list_messages(
        self,
        session_id: str,
        *,
        session_type: SessionType = SessionType.PRIVATE,
        rows: int = 100,
        direction: str = "down",
        min_msg_id: str | None = None,
        include_deleted: bool = True,
    ) -> MessageListResponse:
        if not 1 <= rows <= 100:
            raise ValueError("rows must be between 1 and 100")
        normalized_direction = direction.lower()
        if normalized_direction not in {"up", "down"}:
            raise ValueError("direction must be 'up' or 'down'")
        request: dict[str, Any] = {
            "includeDeleted": include_deleted,
            "rows": rows,
            "direction": normalized_direction,
            "sessionId": session_id,
            "sessionType": session_type.value,
        }
        if min_msg_id:
            request["minMsgId"] = min_msg_id
        payload = await self._request("POST", MESSAGE_LIST, json_body=request)
        data = payload.get("data") or {}
        messages = data.get("messages") or []
        if not isinstance(messages, list):
            raise ProtocolError("message/list messages is not a list")
        return MessageListResponse(messages=messages, has_more=bool(data.get("hasMore")))

    async def mark_message_read(self, session_id: str) -> int:
        payload = await self._request("POST", MARK_MESSAGE_READ, json_body={"groupId": session_id})
        return int(payload.get("data") or 0)

    async def list_unread(
        self,
        *,
        rows: int = 100,
        include_archived: bool = False,
        session_type: SessionType | None = None,
    ) -> UnreadListResponse:
        response = await self.list_chats(rows=rows, include_archived=include_archived)
        unread: list[UnreadChat] = []
        unread_total = 0
        for chat in response.chats:
            raw_count = chat.get("unreadMessageCount", 0)
            try:
                count = max(0, int(raw_count))
            except (TypeError, ValueError):
                count = 0
            if count == 0:
                continue
            raw_session_id = chat.get("sessionId")
            if not isinstance(raw_session_id, str) or not raw_session_id:
                raise ProtocolError("unread chat has no sessionId")
            raw_type = str(chat.get("sessionType") or SessionType.PRIVATE.value).upper()
            current_type = (
                SessionType.GROUP if raw_type == SessionType.GROUP.value else SessionType.PRIVATE
            )
            if session_type is not None and current_type != session_type:
                continue
            raw_name = chat.get("sessionName")
            detail = chat.get("userDetailVo") or {}
            session_name = raw_name or (
                detail.get("nickName") if isinstance(detail, dict) else None
            )
            if not isinstance(session_name, str):
                session_name = None
            last_message = chat.get("lastUnifiedMessage") or chat.get("lastMessage")
            if not isinstance(last_message, dict):
                last_message = None
            raw_message_id = last_message.get("msgId") if last_message else None
            last_message_id = raw_message_id if isinstance(raw_message_id, str) else None
            raw_time = chat.get("lastMessageTime")
            if raw_time is None and last_message:
                raw_time = last_message.get("timestamp")
            try:
                last_message_time = int(raw_time) if raw_time is not None else None
            except (TypeError, ValueError):
                last_message_time = None
            unread.append(
                UnreadChat(
                    session_id=raw_session_id,
                    session_name=session_name,
                    session_type=current_type,
                    unread_count=count,
                    last_message_id=last_message_id,
                    last_message_time=last_message_time,
                    last_message=last_message,
                )
            )
            unread_total += count
        return UnreadListResponse(
            chats=unread,
            total=len(unread),
            unread_total=unread_total,
        )

    async def send_text(
        self,
        session_id: str,
        text: str,
        *,
        session_type: SessionType = SessionType.PRIVATE,
        wait_ack: bool = True,
        local_id: str | None = None,
    ) -> SentMessage:
        if not self.connected:
            raise NotConnectedError(f"client state is {self.state}")
        if not text:
            raise ValueError("text cannot be empty")
        if local_id is None:
            local_id = str(uuid.uuid4())
        else:
            parsed_uuid = uuid.UUID(local_id)
            if parsed_uuid.version != 4 or str(parsed_uuid) != local_id.lower():
                raise ValueError("idempotency key must be a canonical UUID v4")

        now = time()
        self._local_id_fingerprints = {
            key: value for key, value in self._local_id_fingerprints.items() if value[1] > now
        }
        fingerprint = (session_id, session_type.value, text)
        prior = self._local_id_fingerprints.get(local_id)
        if prior is not None and prior[0] != fingerprint:
            raise ValueError("an idempotency key cannot be reused for a different message")
        if local_id in self._pending:
            raise ValueError("a message with this idempotency key is already pending")
        self._local_id_fingerprints[local_id] = (fingerprint, now + 600)

        timestamp = int(time() * 1000)
        frame = {
            "msgType": MessageType.TEXT.value,
            "content": text,
            "localId": local_id,
            "self": True,
            "sessionId": session_id,
            "sessionType": session_type.value,
            "timestamp": timestamp,
            "sendStatus": 0,
            "mentionUsers": None,
            "refMsgIds": None,
        }
        pending: asyncio.Future[dict[str, Any]] | None = None
        if wait_ack:
            pending = asyncio.get_running_loop().create_future()
            self._pending[local_id] = PendingConfirmation(
                future=pending,
                session_id=session_id,
                session_type=session_type,
            )

        try:
            async with self._send_lock:
                ws = self._ws
                if self.state != ClientState.CONNECTED or ws is None:
                    raise NotConnectedError("chat connection was lost before sending")
                await ws.send(json.dumps(frame, ensure_ascii=False, separators=(",", ":")))
        except asyncio.CancelledError:
            self._pending.pop(local_id, None)
            if pending and not pending.done():
                pending.cancel()
            raise
        except Exception as exc:
            self._pending.pop(local_id, None)
            if pending and not pending.done():
                pending.cancel()
            if isinstance(exc, NotConnectedError):
                raise
            raise NotConnectedError("failed to write the message to the chat socket") from exc

        if not wait_ack or pending is None:
            return SentMessage(
                local_id=local_id,
                session_id=session_id,
                session_type=session_type,
                accepted=True,
                timestamp=timestamp,
            )
        try:
            confirmation = await asyncio.wait_for(pending, timeout=self.ack_timeout_seconds)
        except TimeoutError as exc:
            raise MessageAckTimeout(local_id, self.ack_timeout_seconds) from exc
        finally:
            self._pending.pop(local_id, None)

        return SentMessage(
            local_id=local_id,
            session_id=session_id,
            session_type=session_type,
            accepted=True,
            message_id=_optional_string(confirmation.get("msgId")),
            status=_optional_int(confirmation.get("status")),
            sequence=_optional_int(confirmation.get("seqNo")),
            timestamp=_optional_int(confirmation.get("timestamp")) or timestamp,
        )

    async def iter_events(self) -> AsyncIterator[dict[str, Any]]:
        async with self.events.subscribe() as queue:
            while True:
                yield await queue.get()

    def status(self, *, session_path: str | None = None) -> ClientStatus:
        return ClientStatus(
            state=self.state,
            connected=self.connected,
            uid=self.session.uid,
            reconnect_attempts=self.reconnect_attempts,
            last_error=self.last_error,
            session_path=session_path,
        )

    async def _run_forever(self) -> None:
        attempts = 0
        while not self._stop_event.is_set():
            connected_at: float | None = None
            self.reconnect_attempts = attempts
            self.state = ClientState.CONNECTING if attempts == 0 else ClientState.RECONNECTING
            try:
                await self._connect_once()
                connected_at = monotonic()
                self.state = ClientState.CONNECTED
                self.last_error = None
                self._connected_event.set()
                await self._receive_until_closed()
                raise ConnectionError("chat WebSocket closed")
            except asyncio.CancelledError:
                raise
            except SessionExpiredError as exc:
                self.last_error = _safe_error(exc)
                self.state = ClientState.SESSION_EXPIRED
                self._connected_event.clear()
                self._fail_pending(exc)
                await self._close_ws()
                return
            except RateLimitedError as exc:
                self.last_error = _safe_error(exc)
                self.state = ClientState.RATE_LIMITED
                delay = exc.retry_after or self.reconnect_delay_seconds
            except Exception as exc:
                self.last_error = _safe_error(exc)
                delay = self.reconnect_delay_seconds

            self._connected_event.clear()
            self._fail_pending(NotConnectedError("chat connection was lost"))
            await self._close_ws()
            if (
                connected_at is not None
                and monotonic() - connected_at >= self.stable_connection_seconds
            ):
                attempts = 0
            attempts += 1
            self.reconnect_attempts = attempts
            if attempts >= self.max_reconnects:
                self.state = ClientState.ERROR
                return
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
            except TimeoutError:
                pass

    async def _connect_once(self) -> None:
        await self._ensure_http()
        # common/token also works for a guest. A private endpoint is checked first so an
        # expired cookie cannot silently downgrade the client to an anonymous socket.
        await self.get_my_profile()
        _, ws_token = await self.get_ws_token()
        fvideo_id = self.session.headers.get("fvideo-id")
        if not fvideo_id:
            raise ProtocolError("session has no fvideo-id")
        query = urlencode(
            {
                "token": ws_token,
                "clientType": "web",
                "lang": self.session.headers.get("lang", "zh-CN"),
                "fvideo_id": fvideo_id,
                "source": "channel",
                "version": "v4",
            }
        )
        user_agent = self.session.headers.get("user-agent")
        self._ws = await connect(
            f"{WS_URL}?{query}",
            origin=WS_ORIGIN,
            user_agent_header=user_agent,
            open_timeout=15,
            close_timeout=5,
            ping_interval=None,
        )
        self._awaiting_pong = False
        self._missed_pongs = 0

    async def _receive_until_closed(self) -> None:
        ws = self._ws
        if ws is None:
            raise NotConnectedError("chat WebSocket is missing")
        heartbeat = asyncio.create_task(self._heartbeat_loop(ws), name="binance-chat-heartbeat")
        try:
            async for raw in ws:
                await self._handle_frame(raw)
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    async def _heartbeat_loop(self, ws: Any) -> None:
        while not self._stop_event.is_set() and ws is self._ws:
            await asyncio.sleep(self.heartbeat_seconds)
            if self._stop_event.is_set() or ws is not self._ws:
                return
            if self._awaiting_pong:
                self._missed_pongs += 1
            else:
                self._missed_pongs = 0
            if self._missed_pongs >= self.max_missed_pongs:
                await ws.close(code=4000, reason="heartbeat timeout")
                return
            self._awaiting_pong = True
            await ws.send('{"msgType":"HEART_BEAT"}')

    async def _handle_frame(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                return
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return
        if event.get("msgType") == MessageType.HEARTBEAT:
            self._awaiting_pong = False
            self._missed_pongs = 0
            return


        local_id = event.get("localId") or event.get("uuid")
        if isinstance(local_id, str):
            pending = self._pending.get(local_id)
            msg_type = event.get("msgType")
            session_id = event.get("sessionId")
            session_type = event.get("sessionType")
            if pending:
                session_matches = session_id is None or session_id == pending.session_id
                type_matches = session_type is None or session_type == pending.session_type.value
                is_text_confirmation = (
                    msg_type == MessageType.TEXT.value
                    and event.get("self") is True
                    and session_id == pending.session_id
                    and session_type == pending.session_type.value
                )
            else:
                session_matches = False
                type_matches = False
                is_text_confirmation = False
            is_ack_confirmation = (
                msg_type == MessageType.ACK.value
                and event.get("self") is not False
                and session_matches
                and type_matches
            )
            if (
                pending
                and not pending.future.done()
                and (is_text_confirmation or is_ack_confirmation)
            ):
                pending.future.set_result(event)

        await self.events.publish(event)
        for handler in tuple(self._event_handlers):
            try:
                await handler(event)
            except Exception:
                logger.exception("Binance Chat event handler failed")

    async def _ensure_http(self) -> None:
        if self._http is not None:
            return
        headers = dict(self.session.headers)
        headers.setdefault("accept", "application/json")
        headers.setdefault("content-type", "application/json")
        headers.setdefault("origin", BASE_URL)
        headers.setdefault("referer", CHAT_REFERER)
        cookies = httpx.Cookies()
        for cookie in self.session.cookies:
            cookies.set(cookie.name, cookie.value, domain=cookie.domain, path=cookie.path)
        self._http = httpx.AsyncClient(
            base_url=self.session.base_url,
            headers=headers,
            cookies=cookies,
            follow_redirects=False,
            timeout=httpx.Timeout(15),
            transport=self.http_transport,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        await self._ensure_http()
        assert self._http is not None
        response = await self._http.request(method, path, json=json_body, params=params)
        self._sync_response_cookies(response)
        if response.status_code in {401, 403}:
            raise SessionExpiredError(f"private endpoint returned HTTP {response.status_code}")
        if response.status_code == 429:
            retry_after = _parse_retry_after(response.headers.get("retry-after"))
            raise RateLimitedError(retry_after)
        if response.status_code >= 500:
            raise ProtocolError(f"private endpoint returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProtocolError("private endpoint returned non-JSON data") from exc
        if not isinstance(payload, dict):
            raise ProtocolError("private endpoint returned a non-object JSON value")
        code = str(payload.get("code", ""))
        if response.status_code >= 400:
            raise ApiError(code or str(response.status_code), payload.get("message"))
        if not payload.get("success", False) or code != SUCCESS_CODE:
            if code in self.auth_error_codes:
                raise SessionExpiredError(f"Binance authentication error code: {code}")
            raise ApiError(code or "UNKNOWN", payload.get("message"))
        self.session.last_success_at = _iso_now()
        return payload

    def _sync_response_cookies(self, response: httpx.Response) -> None:
        changed = False
        by_key = {(item.name, item.domain, item.path): item for item in self.session.cookies}
        for incoming in response.cookies.jar:
            domain = incoming.domain or ".binance.com"
            if not is_binance_cookie_domain(domain):
                continue
            path = incoming.path or "/"
            key = (incoming.name, domain, path)
            current = by_key.get(key)
            if incoming.expires is not None and incoming.expires <= int(time()):
                if key in by_key:
                    del by_key[key]
                    changed = True
                if incoming.name.lower() == "csrftoken":
                    self.session.headers.pop("csrftoken", None)
                continue
            raw_same_site = incoming.get_nonstandard_attr("SameSite")
            same_site = (
                str(raw_same_site).capitalize()
                if str(raw_same_site).capitalize() in {"Strict", "Lax", "None"}
                else current.same_site
                if current
                else None
            )
            replacement = CookieEntry(
                name=incoming.name,
                value=incoming.value,
                domain=domain,
                path=path,
                secure=bool(incoming.secure),
                expires=incoming.expires,
                http_only=(
                    incoming.has_nonstandard_attr("HttpOnly")
                    or (current.http_only if current else False)
                ),
                same_site=same_site,
                include_subdomains=bool(incoming.domain_initial_dot),
            )
            validate_cookie_entry(replacement)
            if current != replacement:
                by_key[key] = replacement
                changed = True
            if incoming.name.lower() == "csrftoken":
                self.session.headers["csrftoken"] = incoming.value
        if changed:
            self.session.cookies = list(by_key.values())
            if self.session_store:
                self.session_store.save(self.session)

    async def _close_ws(self) -> None:
        if self._ws is None:
            return
        with suppress(Exception):
            await self._ws.close()
        self._ws = None

    def _fail_pending(self, exc: Exception) -> None:
        pending = tuple(self._pending.values())
        self._pending.clear()
        for confirmation in pending:
            if not confirmation.future.done():
                confirmation.future.set_exception(exc)


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    return int(value) if isinstance(value, (int, float)) else None


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _iso_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def _safe_error(exc: BaseException) -> str:
    text = str(exc)
    text = re.sub(r"(?i)(token|fvideo_id)=([^&\s]+)", r"\1=<redacted>", text)
    return text[:500]
