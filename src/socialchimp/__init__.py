"""One simple way to connect your app to social networks.

Everything an app needs is here, at the top level:

    from socialchimp import Post, Media, InMemoryStorage

Two other groups live in their own places, because most people never touch
them:

- Writing a platform: `socialchimp.platform` for what a platform provides,
  and `socialchimp.http` for making the calls. See
  `docs/adding-a-platform.md`.
- Finding installed platforms: `socialchimp.registry`.

See https://github.com/raghulj/socialchimp for documentation.
"""

from socialchimp.client import Account, SocialChimp
from socialchimp.errors import (
    AuthError,
    ConfigError,
    InvalidPostError,
    NetworkError,
    NotAllowedError,
    NotFoundError,
    NotSupportedError,
    PlatformError,
    RateLimitError,
    SignatureError,
    SocialChimpError,
    TokenExpiredError,
)
from socialchimp.events import (
    Dispatcher,
    InMemorySeenUpdates,
    Poller,
    SeenUpdates,
    Update,
    UpdateKind,
    answer_setup_check,
    check_not_too_old,
    poll,
    verify_hmac_sha256,
    verify_shared_secret,
)
from socialchimp.features import (
    Feature,
    Limits,
    TextCount,
    check_option_names,
    check_post,
    count_graphemes,
    measure_text,
)
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
    require_timezone,
)
from socialchimp.registry import available_platforms, register_platform
from socialchimp.storage import (
    InMemoryStorage,
    RunInThread,
    Storage,
    SyncStorage,
    in_a_thread,
    sync_storage,
)
from socialchimp.tokens import TokenManager

__version__ = "0.3.1"

__all__ = [
    "Account",
    "AppCredentials",
    "AuthError",
    "ConfigError",
    "Connection",
    "Dispatcher",
    "Feature",
    "InMemorySeenUpdates",
    "InMemoryStorage",
    "InvalidPostError",
    "Limits",
    "Media",
    "MediaKind",
    "NetworkError",
    "NotAllowedError",
    "NotFoundError",
    "NotSupportedError",
    "PlatformError",
    "Poller",
    "Post",
    "PostResult",
    "PostState",
    "RateLimitError",
    "RawData",
    "RunInThread",
    "SeenUpdates",
    "SignatureError",
    "SocialChimp",
    "SocialChimpError",
    "Storage",
    "SyncStorage",
    "TextCount",
    "Token",
    "TokenExpiredError",
    "TokenManager",
    "Update",
    "UpdateKind",
    "__version__",
    "answer_setup_check",
    "available_platforms",
    "check_not_too_old",
    "check_option_names",
    "check_post",
    "count_graphemes",
    "in_a_thread",
    "measure_text",
    "poll",
    "register_platform",
    "require_timezone",
    "sync_storage",
    "verify_hmac_sha256",
    "verify_shared_secret",
]
