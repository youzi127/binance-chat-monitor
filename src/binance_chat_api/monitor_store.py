from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self


@dataclass(frozen=True, slots=True)
class StoredMessage:
    key: str
    session_id: str
    message_id: str | None
    sequence_no: int | None
    message_time_ms: int
    received_at_ms: int
    sender: str
    content: str
    last_error: str | None


def message_dedupe_key(event: dict[str, Any]) -> str:
    message_id = event.get("msgId") or event.get("messageId")
    if message_id:
        return f"msg:{message_id}"
    session_id = event.get("sessionId") or event.get("session_id") or ""
    sequence = event.get("seqNo") or event.get("sequence") or ""
    timestamp = event.get("timestamp") or event.get("time") or ""
    return f"fallback:{session_id}:{sequence}:{timestamp}"


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _sender_name(event: dict[str, Any]) -> str:
    sender = event.get("sender")
    if isinstance(sender, dict):
        for key in ("nickName", "nickname", "name", "userId", "uid"):
            value = sender.get(key)
            if value not in (None, ""):
                return str(value)
    for key in ("senderName", "senderUid", "fromUid", "userId"):
        value = event.get(key)
        if value not in (None, ""):
            return str(value)
    return "未知"


class MonitorStore:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS monitored_messages (
              dedupe_key TEXT PRIMARY KEY,
              session_id TEXT NOT NULL,
              message_id TEXT,
              sequence_no INTEGER,
              message_time_ms INTEGER NOT NULL,
              received_at_ms INTEGER NOT NULL,
              sender TEXT NOT NULL,
              content TEXT NOT NULL,
              baseline INTEGER NOT NULL DEFAULT 0,
              delivered_at_ms INTEGER,
              last_error TEXT
            )
            """
        )
        self._connection.commit()

    @classmethod
    def open(cls, path: str | Path) -> Self:
        resolved = Path(path).expanduser().resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        return cls(sqlite3.connect(resolved))

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def record_candidate(self, event: dict[str, Any], *, received_at_ms: int) -> bool:
        key = message_dedupe_key(event)
        message_id = event.get("msgId") or event.get("messageId")
        session_id = event.get("sessionId") or event.get("session_id") or ""
        timestamp = _int_or_none(event.get("timestamp") or event.get("time")) or received_at_ms
        sequence = _int_or_none(event.get("seqNo") or event.get("sequence"))
        content = event.get("content") or event.get("message") or event.get("text") or ""
        cursor = self._connection.execute(
            """
            INSERT OR IGNORE INTO monitored_messages (
              dedupe_key, session_id, message_id, sequence_no, message_time_ms,
              received_at_ms, sender, content, baseline
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                key,
                str(session_id),
                str(message_id) if message_id is not None else None,
                sequence,
                timestamp,
                received_at_ms,
                _sender_name(event),
                str(content),
            ),
        )
        self._connection.commit()
        return cursor.rowcount == 1

    def baseline(self, events: list[dict[str, Any]], *, received_at_ms: int) -> None:
        for event in events:
            self.record_candidate(event, received_at_ms=received_at_ms)
            self._connection.execute(
                "UPDATE monitored_messages SET baseline=1 WHERE dedupe_key=?",
                (message_dedupe_key(event),),
            )
        self._connection.commit()

    def contains(self, key: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM monitored_messages WHERE dedupe_key=?",
            (key,),
        ).fetchone()
        return row is not None

    def is_initialized(self) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM monitored_messages LIMIT 1"
        ).fetchone()
        return row is not None

    def mark_delivered(self, key: str, *, delivered_at_ms: int) -> None:
        self._connection.execute(
            "UPDATE monitored_messages SET delivered_at_ms=?, last_error=NULL WHERE dedupe_key=?",
            (delivered_at_ms, key),
        )
        self._connection.commit()

    def mark_failed(self, key: str, error: str) -> None:
        self._connection.execute(
            "UPDATE monitored_messages SET last_error=? WHERE dedupe_key=?",
            (error[:500], key),
        )
        self._connection.commit()

    def pending(self) -> list[StoredMessage]:
        rows = self._connection.execute(
            """
            SELECT dedupe_key, session_id, message_id, sequence_no, message_time_ms,
                   received_at_ms, sender, content, last_error
            FROM monitored_messages
            WHERE baseline=0 AND delivered_at_ms IS NULL
            ORDER BY message_time_ms, dedupe_key
            """
        ).fetchall()
        return [
            StoredMessage(
                key=row["dedupe_key"],
                session_id=row["session_id"],
                message_id=row["message_id"],
                sequence_no=row["sequence_no"],
                message_time_ms=row["message_time_ms"],
                received_at_ms=row["received_at_ms"],
                sender=row["sender"],
                content=row["content"],
                last_error=row["last_error"],
            )
            for row in rows
        ]

    def close(self) -> None:
        self._connection.close()

