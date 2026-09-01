"""Application error types.

Kept deliberately small: the distinction that matters downstream is
"retrying this might work" vs "this will never work".
"""

from __future__ import annotations


class BoomshareError(Exception):
    """Base class for errors raised by this application."""


class RetryableError(BoomshareError):
    """Transient failure - the caller may retry (timeouts, 5xx, rate limits)."""


class PermanentError(BoomshareError):
    """Failure that will not be fixed by retrying (bad request, invalid data)."""


class MetaApiError(BoomshareError):
    def __init__(self, message: str, status_code: int | None = None, payload: object = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class MetaRetryableError(MetaApiError, RetryableError):
    pass


class MetaPermanentError(MetaApiError, PermanentError):
    pass


class AiUnavailableError(RetryableError):
    """OpenAI could not produce a usable decision."""


class MessagingPolicyError(PermanentError):
    """The message we were asked to send is not allowed by Meta's rules."""


class InvalidStateTransition(PermanentError):
    def __init__(self, current: str, target: str):
        super().__init__(f"cannot move sales stage {current} -> {target}")
        self.current = current
        self.target = target
