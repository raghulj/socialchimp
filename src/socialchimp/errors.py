"""One set of errors for every network.

Each network reports problems in its own way. A platform file turns those
into the errors below, so your code catches one thing instead of learning
nine different error formats.

Catch `SocialChimpError` to catch everything socialchimp raises.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AuthError",
    "ConfigError",
    "InvalidPostError",
    "NetworkError",
    "NotAllowedError",
    "NotFoundError",
    "NotSupportedError",
    "PlatformError",
    "RateLimitError",
    "SignatureError",
    "SocialChimpError",
    "TokenExpiredError",
]


class SocialChimpError(Exception):
    """Base for everything socialchimp raises.

    Catch this to catch every problem socialchimp reports, whichever network
    caused it.

    Attributes:
        platform: Which network this came from, when it came from one.
            `None` for problems on your own side, such as `ConfigError`.
        raw: The network's untouched reply, when there was one. Empty
            otherwise. Look here for anything socialchimp did not model.
    """

    def __init__(
        self,
        message: str,
        *,
        platform: str | None = None,
        raw: dict[str, Any] | None = None,
    ) -> None:
        """Record the message, and where it came from.

        Args:
            message: What happened, in plain words.
            platform: Which network complained, if any.
            raw: The network's untouched reply, if there was one.
        """
        super().__init__(message)
        self.platform = platform
        self.raw: dict[str, Any] = raw if raw is not None else {}


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

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
        platform: str | None = None,
        raw: dict[str, Any] | None = None,
    ) -> None:
        """Store the wait time alongside the message.

        Args:
            message: What happened.
            retry_after: Seconds to wait, if the network said.
            platform: Which network is asking us to slow down.
            raw: The network's untouched reply.
        """
        super().__init__(message, platform=platform, raw=raw)
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
        suggestion: What to do instead, when there is something. `None`
            when the answer really is just "not here".
    """

    def __init__(
        self,
        *,
        platform: str,
        what: str,
        suggestion: str | None = None,
    ) -> None:
        """Build a message naming both the network and the missing feature.

        Args:
            platform: The network that cannot do it.
            what: The thing it cannot do. Keep it to a phrase that finishes
                "pinterest does not support ..." - anything longer belongs
                in `suggestion`, or the first sentence runs on for a
                paragraph and nobody reads the end of it.
            suggestion: What to do instead, written as whole sentences. It
                is added after the first one.
        """
        message = f"{platform} does not support {what}."
        if suggestion is not None:
            message = f"{message} {suggestion}"
        super().__init__(message, platform=platform)
        self.what = what
        self.suggestion = suggestion


class NetworkError(SocialChimpError):
    """We could not reach the network at all.

    A connection that dropped, a name that would not resolve, a request that
    ran out of time. socialchimp already tried again several times before
    raising this.

    This is not the network saying no - it never answered. Unlike most
    errors here, trying again later is a reasonable thing to do.
    """


class SignatureError(SocialChimpError):
    """This request did not come from the network it claims to come from.

    Raised when a signature does not match, a shared secret is wrong, a
    required header is missing, or the request is too old to trust. Treat
    every one of these the same way: answer 401 and do nothing else. Do not
    tell the caller which check failed - that only helps whoever is guessing.
    """


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
        super().__init__(message, platform=platform, raw=raw)
        self.status_code = status_code
