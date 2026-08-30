"""One simple way to connect your app to social networks.

See https://github.com/raghulj/socialchimp for documentation.
"""

from socialchimp.errors import (
    AuthError,
    ConfigError,
    InvalidPostError,
    NotAllowedError,
    NotFoundError,
    NotSupportedError,
    PlatformError,
    RateLimitError,
    SocialChimpError,
    TokenExpiredError,
)
from socialchimp.features import Feature, Limits, check_post
from socialchimp.models import (
    AppCredentials,
    Connection,
    Media,
    MediaKind,
    Post,
    PostResult,
    PostState,
    RawData,
    Token,
)
from socialchimp.storage import InMemoryStorage, Storage

__version__ = "0.0.1"

__all__ = [
    "AppCredentials",
    "AuthError",
    "ConfigError",
    "Connection",
    "Feature",
    "InMemoryStorage",
    "InvalidPostError",
    "Limits",
    "Media",
    "MediaKind",
    "NotAllowedError",
    "NotFoundError",
    "NotSupportedError",
    "PlatformError",
    "Post",
    "PostResult",
    "PostState",
    "RateLimitError",
    "RawData",
    "SocialChimpError",
    "Storage",
    "Token",
    "TokenExpiredError",
    "__version__",
    "check_post",
]
