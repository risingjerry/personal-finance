"""User-facing errors for the connectors.

Messages must never contain cookie values or request headers.  The CLI maps
the class to a process exit code so scripts can distinguish "please log in"
from "risk control stopped us" and from a plain network failure.
"""

from __future__ import annotations


class ConnectorError(Exception):
    """Base class for expected, user-readable failures."""

    exit_code = 2

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint

    def render(self) -> str:
        if self.hint:
            return f"{self.message}\n提示: {self.hint}"
        return self.message


class UsageError(ConnectorError):
    exit_code = 2


class CookieError(ConnectorError):
    exit_code = 3


class AuthRequired(ConnectorError):
    exit_code = 3


class BrowserUnavailable(ConnectorError):
    """No usable Playwright Chromium is installed.

    This tool never downloads browsers; it reports exactly what is missing so
    the owner can install it deliberately.
    """

    exit_code = 3


class RiskBlocked(ConnectorError):
    exit_code = 4


class NeedsRiskSignature(RiskBlocked):
    """The server demands an anti-bot signature (wsgsig, mtgsig...).

    The request is well-formed and authenticated, but the server wants a token
    that only the site's own page SDK can produce.  This tool never computes,
    forges or replays one, so it stops with this typed, documented blocker.
    """

    exit_code = 4


# Older site modules use this name for the same condition; keep one class so
# ``except RiskSignatureRequired`` and ``except NeedsRiskSignature`` agree.
RiskSignatureRequired = NeedsRiskSignature


class NetworkError(ConnectorError):
    exit_code = 5


class ApiError(ConnectorError):
    exit_code = 6


def internal_error(exc: Exception) -> str:
    """Only code identifiers: no message, source lines, locals or arguments."""
    tb = exc.__traceback__
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    location = "unknown"
    if tb is not None:
        location = f"{tb.tb_frame.f_globals.get('__name__', 'unknown')}.{tb.tb_frame.f_code.co_name}"
    return f"内部错误: {type(exc).__name__} @ {location}（值已省略）"
