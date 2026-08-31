"""Write a failed post into your own table, then retry it from a worker.

socialchimp raises and stops. It never writes a failure down, never retries in
the background and never decides an account is finished - because carrying on
is a decision, and only your app can make it.

So this is the other half, and it is the half people get stuck on. Three
pieces:

1. A `post_failure` table holding the connection, what was attempted, the
   error **class**, the message, whether it is worth retrying and when.
2. An `except` that fills a row in.
3. A worker that reads the rows that are due and tries them again.

The error class is what decides, not the message. `RateLimitError` is worth
another go and says how long to wait; `NotSupportedError` never is, because
a network that cannot schedule will still not be able to schedule tomorrow.

This example talks to no real network. It uses the fake platform that ships
with socialchimp for testing, so it runs anywhere with no credentials. The
database is sqlite in memory, and the calls to it are blocking - a real app
would use an async database layer, or `sync_storage` around a blocking one.

Run it with:

    uv run python examples/failures_and_retries.py
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta

from socialchimp import (
    InMemoryStorage,
    NetworkError,
    NotSupportedError,
    Post,
    RateLimitError,
    SocialChimp,
    SocialChimpError,
)
from socialchimp.testing import FakePlatform

SCHEMA = """
CREATE TABLE post_failure (
    id             INTEGER PRIMARY KEY,
    connection_id  TEXT    NOT NULL,
    attempted      TEXT    NOT NULL,
    error_type     TEXT    NOT NULL,
    message        TEXT    NOT NULL,
    worth_retrying INTEGER NOT NULL,
    try_again_at   TEXT,
    tries          INTEGER NOT NULL DEFAULT 0
)
"""

# How long to wait when a network asks us to slow down without saying how
# long for.
DEFAULT_RATE_LIMIT_WAIT = 15 * 60

# Stop after this many goes. Without a cap, a failure nobody has modelled is
# retried forever.
MOST_TRIES = 5


def when_to_try_again(refused: SocialChimpError) -> datetime | None:
    """Say when this failure is worth another go. `None` means never."""
    if isinstance(refused, RateLimitError):
        # retry_after is seconds, and None where the network did not say.
        # Written this way rather than `refused.retry_after or DEFAULT`
        # because a network really can answer nought, and nought is an
        # answer.
        wait = (
            DEFAULT_RATE_LIMIT_WAIT
            if refused.retry_after is None
            else refused.retry_after
        )
        return datetime.now(UTC) + timedelta(seconds=wait)
    if isinstance(refused, NetworkError):
        # Nobody answered. socialchimp already tried several times.
        return datetime.now(UTC) + timedelta(minutes=5)
    # Everything else is the post, the code or the person, and none of those
    # gets better on its own.
    return None


def record_failure(
    db: sqlite3.Connection,
    connection_id: str,
    post: Post,
    refused: SocialChimpError,
) -> None:
    """Write one failure down, with enough to try it again later."""
    again = when_to_try_again(refused)
    db.execute(
        "INSERT INTO post_failure (connection_id, attempted, error_type,"
        " message, worth_retrying, try_again_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            connection_id,
            json.dumps({"text": post.text}),
            # The class name, not the message. Messages are written for
            # people and change; the class is what code may depend on.
            type(refused).__name__,
            str(refused),
            int(again is not None),
            None if again is None else again.isoformat(),
        ),
    )
    db.commit()


async def post_or_record(
    sc: SocialChimp,
    db: sqlite3.Connection,
    connection_id: str,
    post: Post,
) -> None:
    """Publish, and write the failure down if there is one."""
    try:
        result = await sc.account(connection_id).post(post)
    except SocialChimpError as refused:
        record_failure(db, connection_id, post, refused)
        print(f"  failed   {connection_id}: {type(refused).__name__}: {refused}")
    else:
        print(f"  posted   {connection_id}: {result.id}")


async def retry_due_failures(sc: SocialChimp, db: sqlite3.Connection) -> None:
    """Try again anything whose `try_again_at` has passed.

    This is the whole of what a background worker does. Put it behind a
    Celery task, an arq job or a cron line - socialchimp depends on no queue,
    so all three are the same call.
    """
    rows = db.execute(
        "SELECT * FROM post_failure WHERE worth_retrying"
        " AND try_again_at <= ? AND tries < ?",
        (datetime.now(UTC).isoformat(), MOST_TRIES),
    ).fetchall()

    for row in rows:
        attempted: dict[str, str] = json.loads(row["attempted"])
        post = Post(text=attempted["text"])
        try:
            await sc.account(row["connection_id"]).post(post)
        except SocialChimpError as refused:
            again = when_to_try_again(refused)
            db.execute(
                "UPDATE post_failure SET tries = tries + 1, error_type = ?,"
                " message = ?, worth_retrying = ?, try_again_at = ?"
                " WHERE id = ?",
                (
                    type(refused).__name__,
                    str(refused),
                    int(again is not None),
                    None if again is None else again.isoformat(),
                    row["id"],
                ),
            )
            print(f"  still failing  {row['connection_id']}: {refused}")
        else:
            db.execute("DELETE FROM post_failure WHERE id = ?", (row["id"],))
            print(f"  went through   {row['connection_id']}")
    db.commit()


def show(db: sqlite3.Connection) -> None:
    """Print what is in the failures table."""
    rows = db.execute("SELECT * FROM post_failure ORDER BY id").fetchall()
    if not rows:
        print("  post_failure is empty")
        return
    for row in rows:
        again = row["try_again_at"] or "never"
        print(
            f"  {row['connection_id']:10} {row['error_type']:18}"
            f" retry={bool(row['worth_retrying'])!s:5} when={again}"
        )


async def main() -> None:
    """Fail two posts for different reasons, then retry only the one worth it."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)

    # One network is over its limit right now, which is a moment. The other
    # cannot do what was asked at all, which is not.
    busy = FakePlatform(
        name="busy",
        publish_fails_with=RateLimitError(
            "Too many posts for now.", retry_after=0.0, platform="busy"
        ),
    )
    cannot = FakePlatform(
        name="cannot",
        publish_fails_with=NotSupportedError(
            platform="cannot", what="posting on a Sunday"
        ),
    )

    storage = InMemoryStorage()
    await storage.save_connection(busy.connection(connection_id="busy-1"))
    await storage.save_connection(cannot.connection(connection_id="cannot-1"))

    async with SocialChimp(
        storage=storage, platforms={"busy": busy, "cannot": cannot}
    ) as sc:
        post = Post(text="We open at nine.")

        print("The web request tries to post:")
        for connection_id in ("busy-1", "cannot-1"):
            await post_or_record(sc, db, connection_id, post)

        print("\nWhat is in the table:")
        show(db)

        # Time passes and the network stops refusing.
        busy.publish_fails_with = None

        print("\nThe worker runs:")
        await retry_due_failures(sc, db)

        print("\nWhat is left:")
        show(db)
        print(
            "\nThe NotSupportedError row is still there and always will be."
            "\nNothing retries it, because retrying cannot help. Somebody has"
            "\nto look at it, which is why it is a row and not a log line."
        )

    db.close()


if __name__ == "__main__":
    asyncio.run(main())
