from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from typing import Any

from .monitor_store import MonitorStore, StoredMessage, message_dedupe_key
from .protocol import SessionType
from .wecom import WeComNotifier, format_chat_notification

logger = logging.getLogger(__name__)


def _message_time_ms(event: dict[str, Any]) -> int:
    value = event.get("timestamp") or event.get("time") or 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def is_target_incoming_text(
    event: dict[str, Any], session_id: str, own_uid: str | int | None = None
) -> bool:
    if event.get("self") is True:
        return False
    if event.get("self") is None and own_uid is not None:
        sender = event.get("sender")
        sender_uid = sender.get("uid") if isinstance(sender, dict) else event.get("senderUid")
        if sender_uid is not None and str(sender_uid) == str(own_uid):
            return False
    return (
        isinstance(event, dict)
        and event.get("msgType") == "U_TEXT"
        and str(event.get("sessionId") or event.get("session_id") or "") == session_id
    )


def _content(event: dict[str, Any]) -> str:
    value = event.get("content") or event.get("message") or event.get("text") or ""
    return value if isinstance(value, str) else str(value)


def _sender(event: dict[str, Any]) -> str:
    sender = event.get("sender")
    if isinstance(sender, dict):
        for key in ("nickName", "nickname", "name", "userId", "uid"):
            value = sender.get(key)
            if value not in (None, ""):
                return str(value)
    return str(event.get("senderName") or event.get("senderUid") or "未知")


class ChatMonitor:
    def __init__(
        self,
        client: Any,
        store: MonitorStore,
        notifier: WeComNotifier,
        target_session_id: str,
        *,
        session_name: str | None = None,
        clock_ms: Callable[[], int] | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.client = client
        self.store = store
        self.notifier = notifier
        self.target_session_id = target_session_id
        self.session_name = session_name or target_session_id
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.sleep = sleep

    def _own_uid(self) -> str | int | None:
        session = getattr(self.client, "session", None)
        return getattr(session, "uid", None)

    async def initialize_baseline(self) -> None:
        history = await self.client.list_messages(
            self.target_session_id,
            rows=100,
            direction="down",
            session_type=SessionType.GROUP,
        )
        events = [
            event
            for event in history.messages
            if is_target_incoming_text(event, self.target_session_id, self._own_uid())
        ]
        if not self.store.is_initialized():
            self.store.baseline(events, received_at_ms=self.clock_ms())
            logger.info(
                "monitor baseline established: session=%s count=%d",
                self.target_session_id,
                len(events),
            )
            return
        for event in sorted(events, key=_message_time_ms):
            await self.process_event(event)

    async def process_event(self, event: dict[str, Any]) -> bool:
        if not is_target_incoming_text(event, self.target_session_id, self._own_uid()):
            return False
        received_at_ms = self.clock_ms()
        if not self.store.record_candidate(event, received_at_ms=received_at_ms):
            return False
        message_time_ms = _message_time_ms(event) or received_at_ms
        text = format_chat_notification(
            session_name=self.session_name,
            sender=_sender(event),
            content=_content(event),
            message_time_ms=message_time_ms,
            received_at_ms=received_at_ms,
        )
        key = message_dedupe_key(event)
        try:
            await self.notifier.send_text(text)
        except Exception as exc:
            self.store.mark_failed(key, type(exc).__name__)
            logger.warning(
                "wecom delivery failed: session=%s key=%s error=%s",
                self.target_session_id,
                key,
                type(exc).__name__,
            )
            return False
        self.store.mark_delivered(key, delivered_at_ms=self.clock_ms())
        logger.info("message delivered: session=%s key=%s", self.target_session_id, key)
        return True

    async def retry_pending(self) -> None:
        for item in self.store.pending():
            await self._deliver_stored(item)

    async def _deliver_stored(self, item: StoredMessage) -> None:
        text = format_chat_notification(
            session_name=self.session_name,
            sender=item.sender,
            content=item.content,
            message_time_ms=item.message_time_ms,
            received_at_ms=item.received_at_ms,
        )
        try:
            await self.notifier.send_text(text)
        except Exception as exc:
            self.store.mark_failed(item.key, type(exc).__name__)
            return
        self.store.mark_delivered(item.key, delivered_at_ms=self.clock_ms())

    async def reconcile_history(self) -> None:
        history = await self.client.list_messages(
            self.target_session_id,
            rows=100,
            direction="down",
            session_type=SessionType.GROUP,
        )
        events = sorted(
            (
                event
                for event in history.messages
                if is_target_incoming_text(event, self.target_session_id, self._own_uid())
            ),
            key=_message_time_ms,
        )
        for event in events:
            await self.process_event(event)

    async def poll_once(self) -> None:
        await self.reconcile_history()

    async def run_events(self) -> None:
        events: AsyncIterator[dict[str, Any]] = self.client.iter_events()
        async for event in events:
            await self.process_event(event)

    async def run(self) -> None:
        await self.initialize_baseline()
        await self.retry_pending()
        await self.run_events()

    async def close(self) -> None:
        with suppress(Exception):
            await self.notifier.close()

