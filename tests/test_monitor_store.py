from binance_chat_api.monitor_store import MonitorStore, message_dedupe_key


def event(message_id: str | None = "m-1", *, content: str = "hello") -> dict:
    return {
        "msgId": message_id,
        "sessionId": "g-1",
        "seqNo": 7,
        "timestamp": 1000,
        "content": content,
        "sender": {"nickName": "Alice"},
    }


def test_store_persists_dedupe_across_reopen(tmp_path):
    path = tmp_path / "monitor.sqlite3"
    message = event()
    with MonitorStore.open(path) as store:
        assert store.record_candidate(message, received_at_ms=2000) is True
        assert store.record_candidate(message, received_at_ms=2001) is False
        store.mark_delivered(message_dedupe_key(message), delivered_at_ms=2100)

    with MonitorStore.open(path) as store:
        assert store.record_candidate(message, received_at_ms=3000) is False
        assert store.pending() == []


def test_baseline_never_creates_pending_delivery(tmp_path):
    with MonitorStore.open(tmp_path / "monitor.sqlite3") as store:
        store.baseline([event("old-1")], received_at_ms=2000)
        assert store.pending() == []
        assert store.contains("msg:old-1")


def test_failed_delivery_remains_pending(tmp_path):
    message = event("m-2")
    with MonitorStore.open(tmp_path / "monitor.sqlite3") as store:
        store.record_candidate(message, received_at_ms=2000)
        store.mark_failed(message_dedupe_key(message), "WeCom HTTP 503")
        pending = store.pending()
        assert [item.key for item in pending] == ["msg:m-2"]
        assert pending[0].last_error == "WeCom HTTP 503"


def test_fallback_key_uses_session_sequence_and_timestamp():
    assert message_dedupe_key(event(None)) == "fallback:g-1:7:1000"


def test_pending_records_preserve_message_fields(tmp_path):
    message = event("m-3", content="important")
    with MonitorStore.open(tmp_path / "monitor.sqlite3") as store:
        store.record_candidate(message, received_at_ms=2500)
        stored = store.pending()[0]
        assert stored.content == "important"
        assert stored.sender == "Alice"
        assert stored.message_time_ms == 1000
        assert stored.received_at_ms == 2500


def test_store_reports_initialized_after_first_baseline(tmp_path):
    with MonitorStore.open(tmp_path / "monitor.sqlite3") as store:
        assert store.is_initialized() is False
        store.baseline([event("old")], received_at_ms=2000)
        assert store.is_initialized() is True

