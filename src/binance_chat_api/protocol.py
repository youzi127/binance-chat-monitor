from __future__ import annotations

from enum import StrEnum

BASE_URL = "https://www.binance.com"
WS_URL = "wss://im.binance.com/binance_chat"
WS_ORIGIN = "https://www.binance.com"
CHAT_REFERER = (
    "https://www.binance.com/zh-CN/chatWidgetEntrance?showAll=1&projectType=square-web-ui"
)

GET_WS_TOKEN = "/bapi/c2c/v1/friendly/binance-chat/common/token"
GET_MY_PROFILE = "/bapi/c2c/v3/private/binance-chat/user/get-my-profile"
CHAT_LIST = "/bapi/c2c/v3/private/binance-chat/chat-list"
MESSAGE_LIST = "/bapi/c2c/v1/friendly/binance-chat/message/list"
MARK_MESSAGE_READ = "/bapi/c2c/v2/private/c2c/chat/mark-message-read"

SUCCESS_CODE = "000000"
HEARTBEAT_SECONDS = 15.0
MAX_MISSED_PONGS = 3
RECONNECT_DELAY_SECONDS = 5.0
MAX_RECONNECTS = 5


class MessageType(StrEnum):
    TEXT = "U_TEXT"
    ACK = "U_ACK"
    SYSTEM = "U_SYSTEM"
    HEARTBEAT = "HEART_BEAT"


class SessionType(StrEnum):
    PRIVATE = "PRIVATE"
    GROUP = "GROUP"


class ClientState(StrEnum):
    NO_SESSION = "NO_SESSION"
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    RATE_LIMITED = "RATE_LIMITED"
    ERROR = "ERROR"
    STOPPED = "STOPPED"


AUTH_HEADER_ALLOWLIST = frozenset(
    {
        "accept",
        "bnc-level",
        "bnc-location",
        "bnc-time-zone",
        "bnc-uuid",
        "c2ctype",
        "clienttype",
        "content-type",
        "csrftoken",
        "device-info",
        "fvideo-id",
        "fvideo-token",
        "lang",
        "referer",
        "user-agent",
        "x-passthrough-token",
    }
)

REQUIRED_PRIVATE_HEADERS = frozenset(
    {
        "bnc-uuid",
        "clienttype",
        "csrftoken",
        "device-info",
        "fvideo-id",
        "fvideo-token",
        "lang",
        "x-passthrough-token",
    }
)

SENSITIVE_HEADER_NAMES = frozenset(
    {
        "authorization",
        "bnc-uuid",
        "cookie",
        "csrftoken",
        "device-info",
        "fvideo-id",
        "fvideo-token",
        "x-passthrough-token",
    }
)
