from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

from .protocol import ClientState, SessionType


class SendMessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=10_000)
    target: str | None = Field(
        default=None,
        description="Exact session name, nickname, chat ID, or user ID from /v1/chats",
    )
    session_id: str | None = None
    session_type: SessionType = SessionType.PRIVATE
    wait_ack: bool = True
    idempotency_key: str | None = Field(
        default=None,
        description=(
            "Optional UUID v4 used as localId; reuse only to retry the same logical message"
        ),
    )

    @model_validator(mode="after")
    def validate_target(self) -> SendMessageRequest:
        if bool(self.target) == bool(self.session_id):
            raise ValueError("provide exactly one of target or session_id")
        return self


class SentMessage(BaseModel):
    local_id: str
    session_id: str
    session_type: SessionType
    accepted: bool
    message_id: str | None = None
    status: int | None = None
    sequence: int | None = None
    timestamp: int


class ClientStatus(BaseModel):
    state: ClientState
    connected: bool
    uid: int | None = None
    reconnect_attempts: int = 0
    last_error: str | None = None
    session_path: str | None = None


class CookieImportResponse(BaseModel):
    saved: bool
    cookie_count: int = Field(ge=1)
    status: ClientStatus


class HealthStatus(BaseModel):
    ok: bool = True
    state: ClientState
    connected: bool


class ChatListResponse(BaseModel):
    chats: list[dict[str, Any]]
    total: int


class MessageListResponse(BaseModel):
    messages: list[dict[str, Any]]
    has_more: bool


class UnreadChat(BaseModel):
    session_id: str
    session_name: str | None = None
    session_type: SessionType
    unread_count: int = Field(ge=1)
    last_message_id: str | None = None
    last_message_time: int | None = None
    last_message: dict[str, Any] | None = None


class UnreadListResponse(BaseModel):
    chats: list[UnreadChat]
    total: int = Field(ge=0, description="Number of returned unread sessions")
    unread_total: int = Field(ge=0, description="Sum of unreadMessageCount")


class MarkReadResponse(BaseModel):
    session_id: str
    marked: bool = True
    result: int
