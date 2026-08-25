"""Exception hierarchy shared by every layer."""

from __future__ import annotations


class HarmonyError(Exception):
    """Base class for all errors raised by Harmony itself."""


class ProviderError(HarmonyError):
    """A backend service failed or returned something unusable."""


class AuthError(ProviderError):
    """Credentials are missing, rejected, or expired."""


class MissingCredentialError(HarmonyError):
    """An optional integration was used without being configured."""

    def __init__(self, what: str, hint: str = "") -> None:
        self.what = what
        self.hint = hint
        msg = f"{what} is not configured."
        if hint:
            msg = f"{msg} {hint}"
        super().__init__(msg)


class NotSupportedError(HarmonyError):
    """The operation has no equivalent on this service."""


class RateLimitedError(ProviderError):
    """The service asked us to slow down."""

    def __init__(self, message: str = "Rate limited", retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after
