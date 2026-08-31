"""The Facebook Page story, start to finish, with nothing real involved.

This is the code behind `docs/use-cases/facebook-django.md`, with the
Facebook half replaced by a fake so it runs anywhere, with no app, no review
and no business verification. It shows the four things that use case is
about:

1. Storage written as ordinary blocking database code and handed to
   `sync_storage`. On Django you swap `sync_storage` for
   `socialchimp.contrib.django.orm_storage` and the sqlite below for your
   model - nothing else changes.
2. A sign-in that stops half way to ask **which page**, which is
   `ChooseAccount` followed by `sc.choose(...)`.
3. A scheduled post coming back as `PostState.SCHEDULED` rather than live.
4. A comment arriving as a signed webhook, checked and handed to a
   `Dispatcher`.

Run it with:

    uv run python examples/facebook_django/page_post_demo.py

**Why there is no Django in this directory.** Everything under `examples/`
is checked by `mypy --strict`, and Django ships no type information, so a
file here that imported Django could not be checked without an ignore. The
Django project itself - the model, the storage, the admin action, the views
- is written out in `docs/use-cases/facebook-django.md`. The socialchimp
half is identical either way, and that half is what this file runs.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta

from socialchimp import (
    AppCredentials,
    Connection,
    Dispatcher,
    Feature,
    Post,
    PostResult,
    PostState,
    SocialChimp,
    Token,
    Update,
    UpdateKind,
    sync_storage,
)
from socialchimp.platform import AccountChoice, ChooseAccount, Finished, SendToNetwork
from socialchimp.testing import FakePlatform

# Where the person is sent back to after approving the app. On a real
# Facebook app this has to match the address typed into the developer
# portal, character for character.
REDIRECT_URI = "http://localhost:8000/social/callback/facebook"

# Meta signs every webhook with your app secret. Ours is made up, because
# the fake below checks it the same way Facebook does but with no Facebook.
APP_SECRET = "not-a-real-app-secret"  # noqa: S105


# ---------------------------------------------------------------------------
# 1. Storage: five methods, written the ordinary blocking way.
#
# This is sqlite through the standard library rather than the Django ORM, so
# the file runs on its own - but it is the same five methods, in the same
# shape, with the same three rules: return None when there is nothing,
# delete quietly, and key app credentials by platform *and* host.
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS social_account (
    id            TEXT PRIMARY KEY,
    platform      TEXT NOT NULL,
    host          TEXT,
    account_id    TEXT NOT NULL,
    account_name  TEXT NOT NULL,
    access_token  TEXT NOT NULL,
    refresh_token TEXT,
    expires_at    TEXT,
    scopes        TEXT NOT NULL,
    extra         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS social_app (
    platform      TEXT NOT NULL,
    host          TEXT,
    client_id     TEXT NOT NULL,
    client_secret TEXT NOT NULL,
    PRIMARY KEY (platform, host)
);
"""


class PageStorage:
    """Connections and app credentials, kept in sqlite.

    Nothing here is async. `sync_storage` runs each method on a spare
    thread, so a slow query does not stop everything else in the air at the
    same time.
    """

    def __init__(self, database: str = ":memory:") -> None:
        self._db = sqlite3.connect(database, check_same_thread=False)
        self._db.executescript(SCHEMA)

    def get_connection(self, connection_id: str) -> Connection | None:
        row = self._db.execute(
            "SELECT platform, host, account_id, account_name, access_token, "
            "refresh_token, expires_at, scopes, extra "
            "FROM social_account WHERE id = ?",
            (connection_id,),
        ).fetchone()
        # Nothing stored is not an error. Say so with None and let the
        # caller decide - raising here would turn "not connected yet" into
        # a traceback.
        if row is None:
            return None

        expires_at = datetime.fromisoformat(row[6]) if row[6] else None
        return Connection(
            id=connection_id,
            platform=row[0],
            host=row[1],
            account_id=row[2],
            account_name=row[3],
            token=Token(
                access_token=row[4],
                refresh_token=row[5],
                expires_at=expires_at,
            ),
            scopes=tuple(json.loads(row[7])),
            extra=json.loads(row[8]),
        )

    def save_connection(self, connection: Connection) -> None:
        # Called when an account is first connected, and again after every
        # token renewal - so it has to replace, not insert.
        token = connection.token
        self._db.execute(
            "INSERT OR REPLACE INTO social_account "
            "(id, platform, host, account_id, account_name, access_token, "
            " refresh_token, expires_at, scopes, extra) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                connection.id,
                connection.platform,
                connection.host,
                connection.account_id,
                connection.account_name,
                token.access_token,
                token.refresh_token,
                token.expires_at.isoformat() if token.expires_at else None,
                json.dumps(list(connection.scopes)),
                json.dumps(connection.extra),
            ),
        )
        self._db.commit()

    def delete_connection(self, connection_id: str) -> None:
        # Quiet when it is already gone. Retries happen.
        self._db.execute("DELETE FROM social_account WHERE id = ?", (connection_id,))
        self._db.commit()

    def get_app(self, platform: str, host: str | None) -> AppCredentials | None:
        row = self._db.execute(
            "SELECT client_id, client_secret FROM social_app "
            "WHERE platform = ? AND host IS ?",
            (platform, host),
        ).fetchone()
        if row is None:
            return None
        return AppCredentials(
            platform=platform,
            host=host,
            client_id=row[0],
            client_secret=row[1],
        )

    def save_app(self, app: AppCredentials) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO social_app "
            "(platform, host, client_id, client_secret) VALUES (?, ?, ?, ?)",
            (app.platform, app.host, app.client_id, app.client_secret),
        )
        self._db.commit()


# ---------------------------------------------------------------------------
# 2. A stand-in for Facebook.
#
# `FakePlatform` is the one class in socialchimp you subclass rather than
# implement. Out of the box it cannot schedule, so this adds the feature and
# the one behaviour that goes with it. Giving it `accounts` makes signing in
# stop to ask which page, exactly as the real Facebook platform does.
# ---------------------------------------------------------------------------


class PretendFacebook(FakePlatform):
    """A fake that behaves like Facebook Pages in the two ways that matter."""

    def __init__(self) -> None:
        super().__init__(
            name="facebook",
            features=(
                Feature.POST_TEXT
                | Feature.POST_IMAGE
                | Feature.SCHEDULE
                | Feature.DELETE_POST
                | Feature.PUSH_UPDATES
            ),
            accounts=(
                AccountChoice(id="1001", name="Bench & Bloom", kind="page"),
                AccountChoice(id="1002", name="Bench & Bloom Studio", kind="page"),
            ),
            secret=APP_SECRET,
        )

    async def publish(self, connection: Connection, post: Post) -> PostResult:
        result = await super().publish(connection, post)
        if post.publish_at is None:
            return result
        # Facebook takes a scheduled post now and puts it out later, so
        # there is no address for it yet - it is not on the page.
        return PostResult(id=result.id, url=None, state=PostState.SCHEDULED)


# ---------------------------------------------------------------------------
# 3. What to do when somebody comments.
# ---------------------------------------------------------------------------


async def someone_commented(update: Update) -> None:
    """React to a new comment on the page."""
    said = update.raw.get("message", "")
    print(f"  comment on {update.connection_id}: {said!r}")


async def main() -> None:
    """Connect a page, post to it, schedule a post, receive a comment."""
    facebook = PretendFacebook()
    storage = sync_storage(PageStorage())

    dispatcher = Dispatcher()
    dispatcher.on(UpdateKind.COMMENT_CREATED, someone_commented)

    # Passing the platform in by name is how a test swaps a fake for the
    # real thing - and how you keep a typed handle on a platform when you
    # need one of its own methods, such as `check_signature` below.
    async with SocialChimp(storage=storage, platforms={"facebook": facebook}) as sc:
        # Meta will not register an app for you. On a real app these two
        # values come out of the developer portal by hand, once.
        await sc.storage.save_app(
            AppCredentials(
                platform="facebook",
                host=None,
                client_id="1234567890",
                client_secret=APP_SECRET,
            )
        )

        # --- Half one of the sign-in: send the person to Facebook. ---
        step = await sc.start_login(
            "facebook",
            redirect_uri=REDIRECT_URI,
            state="staff-member-7",
        )
        if not isinstance(step, SendToNetwork):
            message = f"Expected to be sent to Facebook, got {step!r}."
            raise RuntimeError(message)
        print(f"Send the person to: {step.url}")
        # Keep this with their session. It cannot live in memory: they may
        # be sent away by one web worker and come back to another.
        remember = step.remember

        # --- Half two: they came back with a code in the query string. ---
        step = await sc.finish_login(
            "facebook",
            callback={"code": "the-code-facebook-sent-back", "state": step.state},
            redirect_uri=REDIRECT_URI,
            state="staff-member-7",
            remember=remember,
        )

        # Facebook never finishes here. It asks which page - even when the
        # person manages only one, so your app has one path and not two.
        if not isinstance(step, ChooseAccount):
            message = f"Expected to be asked which page, got {step!r}."
            raise RuntimeError(message)
        print("\nWhich page?")
        for option in step.options:
            print(f"  {option.id}  {option.name} ({option.kind})")

        # --- Half three: they picked one. ---
        step = await sc.choose(
            "facebook",
            account_id="1001",
            # This carries the person's own Facebook token. Keep it with
            # their session, never in a URL or a hidden form field.
            resume_token=step.resume_token,
            redirect_uri=REDIRECT_URI,
            state="staff-member-7",
            remember=remember,
        )
        if not isinstance(step, Finished):
            message = f"Expected a finished connection, got {step!r}."
            raise RuntimeError(message)

        connection = step.connection
        print(f"\nConnected {connection.account_name} as {connection.id}.")

        # --- Post now. ---
        account = sc.account(connection.id)
        now = await account.post(Post(text="We are open until six today."))
        print(f"posted    {now.id}  state={now.state.name}")

        # --- Post on Friday. Facebook really does schedule. ---
        friday = datetime.now(UTC) + timedelta(days=2)
        later = await account.post(
            Post(text="Half price on Saturday.", publish_at=friday)
        )
        # SCHEDULED, and no url: Facebook has taken a plan, not published a
        # post, so there is nothing on the page to link to yet.
        print(f"scheduled {later.id}  state={later.state.name}  url={later.url}")

        # --- A comment arrives. ---
        # In a real app this body is the raw bytes of Meta's request, and
        # the signature is in a header. Never parse the JSON before
        # checking: a signature is over the exact bytes that were sent.
        body = json.dumps(
            {
                "id": "comment-1",
                "kind": "comment_created",
                "connection_id": connection.id,
                "at": datetime.now(UTC).isoformat(),
                "message": "Do you deliver?",
            }
        ).encode()
        headers = facebook.sign(body)

        print("\nA webhook arrives:")
        facebook.check_signature(body, headers, secret=APP_SECRET)
        await dispatcher.deliver(facebook.read_update(body, headers))


if __name__ == "__main__":
    asyncio.run(main())
