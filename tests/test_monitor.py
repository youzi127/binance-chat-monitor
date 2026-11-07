from __future__ import annotations

from dataclasses import dataclass

import pytest

from binance_chat_api.monitor import ChatMonitor, is_target_incoming_text
from binance_chat_api.monitor_store import MonitorStore
from binance_chat_api.protocol import SessionType


def incoming(message_id: str, timestamp: int, *, content: str | None = None) -> dict:
    return {
        "msgType": "U_TEXT",
        "self": False,
        "sessionId": "target",
        "msgId": message_id,
        "seqNo": timestamp,
        "timestamp": timestamp,
        "content": content or message_id,
        "sender": {"nickName": "Alice"},
    }


@dataclass
class History:
    messages: list[dict]


class FakeClient:
    def __init__(self, history: list[dict] | None = None):
        self.history = history or []

    async def list_messages(self, *_args, **_kwargs):
        assert _kwargs["session_type"] is SessionType.GROUP
        return History(self.history)


class FakeNotifier:
    def __init__(self):
        self.messages: list[str] = []

    async def send_text(self, text: str):
        self.messages.append(text)


def test_filter_accepts_only_target_incoming_text():
    assert is_target_incoming_text(incoming("m", 1), "target")
    assert not is_target_incoming_text({**incoming("m", 1), "self": True}, "target")
    assert not is_target_incoming_text({**incoming("m", 1), "msgType": "U_IMAGE"}, "target")
    assert not is_target_incoming_text({**incoming("m", 1), "sessionId": "other"}, "target")
    event = {**incoming("m", 1), "self": None, "sender": {"uid": "other"}}
    assert is_target_incoming_text(event, "target", own_uid="me")
    own = {**event, "sender": {"uid": "me"}}
    assert not is_target_incoming_text(own, "target", own_uid="me")


@pytest.mark.asyncio
async def test_first_history_is_baseline_and_not_sent(tmp_path):
    notifier = FakeNotifier()
    with MonitorStore.open(tmp_path / "monitor.sqlite3") as store:
        monitor = ChatMonitor(
            FakeClient([incoming("old-1", 1000)]),
            store,
            notifier,
            "target",
            clock_ms=lambda: 2000,
        )
        await monitor.initialize_baseline()
        assert notifier.messages == []
        assert store.pending() == []


@pytest.mark.asyncio
async def test_process_event_delivers_once_and_marks_success(tmp_path):
    notifier = FakeNotifier()
    message = incoming("new-1", 1000, content="hello")
    with MonitorStore.open(tmp_path / "monitor.sqlite3") as store:
        monitor = ChatMonitor(FakeClient(), store, notifier, "target", clock_ms=lambda: 3000)
        await monitor.process_event(message)
        await monitor.process_event(message)
        assert len(notifier.messages) == 1
        assert notifier.messages[0].endswith("\nhello")
        assert store.pending() == []


@pytest.mark.asyncio
async def test_reconcile_sends_only_unseen_messages_in_timestamp_order(tmp_path):
    history = [incoming("m-3", 3000), incoming("m-2", 2000), incoming("old", 1000)]
    notifier = FakeNotifier()
    with MonitorStore.open(tmp_path / "monitor.sqlite3") as store:
        store.baseline([incoming("old", 1000)], received_at_ms=1000)
        monitor = ChatMonitor(FakeClient(history), store, notifier, "target", clock_ms=lambda: 4000)
        await monitor.reconcile_history()
        assert [text.rsplit("\n", 1)[1] for text in notifier.messages] == ["m-2", "m-3"]


@pytest.mark.asyncio
async def test_poll_once_reconciles_history_without_websocket_events(tmp_path):
    notifier = FakeNotifier()
    with MonitorStore.open(tmp_path / "monitor.sqlite3") as store:
        monitor = ChatMonitor(
            FakeClient([incoming("poll-1", 5000)]),
            store,
            notifier,
            "target",
            clock_ms=lambda: 6000,
        )
        await monitor.initialize_baseline()
        monitor.client.history.append(incoming("poll-2", 7000, content="new"))
        await monitor.poll_once()
        assert len(notifier.messages) == 1
        assert notifier.messages[0].endswith("\nnew")


@pytest.mark.asyncio
async def test_run_uses_websocket_only_without_polling(tmp_path):
    class EventClient(FakeClient):
        def __init__(self):
            super().__init__([incoming("baseline", 1000)])
            self.poll_calls = 0

        async def list_messages(self, *_args, **_kwargs):
            self.poll_calls += 1
            return await super().list_messages(*_args, **_kwargs)

        async def iter_events(self):
            yield incoming("live", 2000, content="from websocket")
            await asyncio.sleep(60)

    import asyncio

    notifier = FakeNotifier()
    client = EventClient()
    with MonitorStore.open(tmp_path / "monitor.sqlite3") as store:
        monitor = ChatMonitor(client, store, notifier, "target", clock_ms=lambda: 3000)
        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert client.poll_calls == 1
    assert len(notifier.messages) == 1


@pytest.mark.asyncio
async def test_existing_store_does_not_rebaseline_new_history(tmp_path):
    notifier = FakeNotifier()
    with MonitorStore.open(tmp_path / "monitor.sqlite3") as store:
        store.baseline([incoming("old", 1000)], received_at_ms=1000)
        monitor = ChatMonitor(
            FakeClient([incoming("old", 1000), incoming("new", 2000, content="new")]),
            store,
            notifier,
            "target",
            clock_ms=lambda: 3000,
        )
        await monitor.initialize_baseline()
        assert len(notifier.messages) == 1
        assert notifier.messages[0].endswith("\nnew")

