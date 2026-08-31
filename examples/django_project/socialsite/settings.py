"""Settings for the sample project.

Small on purpose. There is no admin, no user model and no static files
pipeline, because none of them has anything to do with posting to a social
network - and a settings file you can read in one go is worth more here than
one that looks like production.

The three things that are not boilerplate:

- `USE_TZ = True`, which socialchimp needs. It refuses a datetime with no
  timezone at the door, because a naive one compares wrongly against an
  aware one and the failure is silent.
- `SOCIALCHIMP`, which names the storage class. `SYNC_STORAGE` is the one to
  use on Django: it means "five ordinary blocking methods", and
  `socialchimp.contrib.django.get_client()` wraps them in `orm_storage` for
  you.
- `SOCIAL_APPS`, the app id and secret for each network, read from the
  environment. Seven of the nine need one, and every one of those seven was
  typed into a developer portal by a human. See `.env.example`.
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _read_env_file(path: Path) -> None:
    """Put `KEY=value` lines from a file into the environment.

    Twenty lines instead of a dependency. A real project would use
    django-environ or python-dotenv; this one avoids anything that is not
    already installed, so `uv run python manage.py check` works with nothing
    else to install.

    Anything already in the environment wins, so
    `FACEBOOK_APP_ID=... python manage.py ...` overrides the file.

    Args:
        path: The file to read. Missing is fine and does nothing.
    """
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        os.environ.setdefault(name.strip(), value.strip().strip("'\""))


_read_env_file(BASE_DIR / ".env")


def _app(id_name: str, secret_name: str) -> dict[str, str] | None:
    """Read one network's app credentials out of the environment.

    Args:
        id_name: The variable holding the public half.
        secret_name: The variable holding the private half.

    Returns:
        Both halves, or `None` when either is missing. `None` is the honest
        answer: the storage class hands it straight back to socialchimp,
        which then says "your app is not registered with this network yet"
        rather than sending an empty client id to the network and getting an
        error about something else.
    """
    client_id = os.environ.get(id_name, "")
    client_secret = os.environ.get(secret_name, "")
    if not client_id or not client_secret:
        return None
    return {"client_id": client_id, "client_secret": client_secret}


# ---------------------------------------------------------------------------
# Ordinary Django.
# ---------------------------------------------------------------------------

# Fine for a sample you run on your laptop. Generate a real one for anything
# else: python -c "import secrets; print(secrets.token_urlsafe(50))"
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "not-a-secret-run-this-locally-only")

DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"

# A tunnel's hostname goes here while you are testing webhooks, because the
# networks that push will not reach localhost.
ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if host.strip()
]

# Django rejects a POST whose Origin is not in this list once it is served
# over https, which is exactly the case while a tunnel is pointed at you.
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "social",
]

MIDDLEWARE = [
    "django.middleware.common.CommonMiddleware",
    # The session is where a half-finished sign-in waits. Both halves of a
    # sign-in are separate requests and can be answered by different web
    # workers, so `remember` and `resume_token` cannot live in a variable.
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "socialsite.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]

WSGI_APPLICATION = "socialsite.wsgi.application"
ASGI_APPLICATION = "socialsite.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LANGUAGE_CODE = "en-gb"
TIME_ZONE = "UTC"

# socialchimp refuses a datetime with no timezone. Leave this on.
USE_TZ = True

# ---------------------------------------------------------------------------
# socialchimp.
# ---------------------------------------------------------------------------

# Where connections live. `SYNC_STORAGE` means five ordinary blocking
# methods written as Django ORM code; `socialchimp.contrib.django` wraps
# them in `orm_storage`, which runs them back on the thread the request
# arrived on. That is the whole configuration socialchimp needs.
SOCIALCHIMP = {"SYNC_STORAGE": "social.storage.ConnectionStorage"}

# Where each network sends people back to. `{platform}` is filled in per
# network, and the finished address has to match what is typed into that
# network's developer portal, character for character - a trailing slash is
# a different address as far as every one of them is concerned.
SOCIAL_REDIRECT_URI = os.environ.get(
    "SOCIAL_REDIRECT_URI", "http://localhost:8000/social/callback/{platform}"
)

# Your app's identity on each network, or None where nothing is configured.
#
# Mastodon is missing from this list on purpose: it is the one network
# socialchimp can register an app on for you, so its credentials arrive
# through `Storage.save_app` and live in the SocialApp table - one row per
# server, because every Mastodon server is a separate place.
#
# Bluesky is missing too, for the opposite reason: it has no app at all.
# `social/storage.py` explains what it hands back instead.
SOCIAL_APPS = {
    "facebook": _app("FACEBOOK_APP_ID", "FACEBOOK_APP_SECRET"),
    # Instagram publishes through the same Meta app as Facebook Pages, so
    # this is usually the same pair of values.
    "instagram": _app("INSTAGRAM_APP_ID", "INSTAGRAM_APP_SECRET"),
    # Threads is *not* the same pair, even inside one Meta app. Adding the
    # Threads use case creates a second id and secret, and using the
    # Facebook pair here fails at the very last step of the sign-in with a
    # message that mentions none of this.
    "threads": _app("THREADS_APP_ID", "THREADS_APP_SECRET"),
    # TikTok calls these the client key and the client secret. The client
    # key is the id.
    "tiktok": _app("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET"),
    "youtube": _app("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET"),
    "x": _app("X_CLIENT_ID", "X_CLIENT_SECRET"),
    "pinterest": _app("PINTEREST_APP_ID", "PINTEREST_APP_SECRET"),
}

# The token Meta quotes back once, when you save a webhook address in its
# form. You invent it. It is not the app secret, and mixing the two up is
# the usual reason Meta says the URL could not be verified without saying
# why. TikTok has no equivalent - it starts sending straight away.
SOCIAL_WEBHOOK_TOKENS = {
    "facebook": os.environ.get("FACEBOOK_VERIFY_TOKEN", ""),
    "instagram": os.environ.get("INSTAGRAM_VERIFY_TOKEN", ""),
    "threads": os.environ.get("THREADS_VERIFY_TOKEN", ""),
}
