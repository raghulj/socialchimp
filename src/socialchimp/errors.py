"""One set of errors for every network.

Each network reports problems in its own way. A platform file turns those
into the errors below, so your code catches one thing instead of learning
fifteen different error formats.

Catch `SocialChimpError` to catch everything socialchimp raises.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AuthError",
    "ConfigError",
    "InvalidPostError",
    "NotAllowedError",
    "NotFoundError",
    "NotSupportedError",
    "PlatformError",
    "RateLimitError",
    "SocialChimpError",
    "TokenExpiredError",
]


class SocialChimpError(Exception):
    """Base for everything socialchimp raises."""


class ConfigError(SocialChimpError):
    """Something is set up wrong on your side.

    Missing credentials, an unknown platform name, a storage class that does
    not do what it promised. These are bugs to fix, not conditions to retry.
    """


class AuthError(SocialChimpError):
    """The network would not accept who we say we are.

    Usually the person needs to connect their account again.
    """


class TokenExpiredError(AuthError):
    """The token ran out and could not be renewed.

    socialchimp renews tokens for you. Seeing this means renewal was not
    possible - the network has no refresh token, or the refresh token itself
    expired or was revoked. The person has to sign in again.
    """


class NotAllowedError(SocialChimpError):
    """The account is real but is not permitted to do this.

    Nearly always a missing permission. Ask for the right one when the
    person connects their account.
    """


class NotFoundError(SocialChimpError):
    """The post, account or page asked for does not exist."""


class RateLimitError(SocialChimpError):
    """The network is asking us to slow down.

    Attributes:
        retry_after: Seconds to wait before trying again, when the network
            tells us. `None` when it does not.
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        """Store the wait time alongside the message.

        Args:
            message: What happened.
            retry_after: Seconds to wait, if the network said.
        """
        super().__init__(message)
        self.retry_after = retry_after


class InvalidPostError(SocialChimpError):
    """The post breaks a rule of the network it was going to.

    Text too long, too many pictures, a missing setting that network needs.
    socialchimp raises this before sending where it can, so you get a clear
    message instead of the network's error code.
    """


class NotSupportedError(SocialChimpError):
    """This network genuinely cannot do that.

    Not a gap in socialchimp - a gap in the network. Bluesky has no
    scheduling; YouTube has no text-only post. Rather than quietly doing
    something else, we say so.

    Attributes:
        platform: The network that cannot do it.
        what: The thing it cannot do, in plain words.
    """

    def __init__(self, *, platform: str, what: str) -> None:
        """Build a message naming both the network and the missing feature.

        Args:
            platform: The network that cannot do it.
            what: The thing it cannot do.
        """
        super().__init__(f"{platform} does not support {what}.")
        self.platform = platform
        self.what = what


class PlatformError(SocialChimpError):
    """The network returned an error we do not have a better name for.

    The original reply is kept on `raw` so you can look at what actually
    happened. If a particular error shows up often, it is worth teaching the
    platform file to raise something more specific.

    Attributes:
        platform: Which network complained.
        status_code: HTTP status, when there was one.
        raw: The network's untouched reply.
    """

    def __init__(
        self,
        message: str,
        *,
        platform: str,
        status_code: int | None = None,
        raw: dict[str, Any] | None = None,
    ) -> None:
        """Keep the network's own reply alongside our message.

        Args:
            message: What happened, in our words.
            platform: Which network complained.
            status_code: HTTP status, when there was one.
            raw: The network's untouched reply.
        """
        super().__init__(message)
        self.platform = platform
        self.status_code = status_code
        self.raw: dict[str, Any] = raw if raw is not None else {}
