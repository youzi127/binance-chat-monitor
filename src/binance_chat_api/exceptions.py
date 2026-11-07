from __future__ import annotations


class BinanceChatError(Exception):
    """Base exception for the client."""


class SessionError(BinanceChatError):
    """The imported browser session cannot be used safely."""


class SessionSecurityError(SessionError):
    """The session file or imported cURL violates a security invariant."""


class SessionExpiredError(SessionError):
    """Binance no longer accepts the imported login session."""


class BrowserBootstrapRetryableError(SessionError):
    """A temporary browser bootstrap failure that is safe to retry."""


class BrowserBootstrapTimeoutError(BrowserBootstrapRetryableError):
    def __init__(self, stage: str, timeout: float) -> None:
        self.stage = stage
        self.timeout = timeout
        super().__init__(f"temporary Chrome timed out during {stage} after {timeout:g}s")


class BrowserBootstrapTransientError(BrowserBootstrapRetryableError):
    """A transient Playwright/Chrome failure that is safe to retry."""


class ProtocolError(BinanceChatError):
    """The private web protocol returned an unexpected response."""


class ApiError(ProtocolError):
    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        self.message = message
        super().__init__(f"Binance API error {code}: {message or 'no message'}")


class RateLimitedError(ProtocolError):
    def __init__(self, retry_after: float | None = None) -> None:
        self.retry_after = retry_after
        detail = f"; retry after {retry_after}s" if retry_after is not None else ""
        super().__init__(f"Binance rate limit reached{detail}")


class NotConnectedError(BinanceChatError):
    """A live chat WebSocket is required for the requested operation."""


class TargetNotFoundError(BinanceChatError):
    """No unique chat matched a requested target."""


class MessageAckTimeout(BinanceChatError):
    def __init__(self, local_id: str, timeout: float) -> None:
        self.local_id = local_id
        self.timeout = timeout
        super().__init__(
            f"Message {local_id} was written to the socket but no server confirmation "
            f"arrived within {timeout}s; delivery is unknown"
        )
