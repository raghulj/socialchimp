"""The sqlite database, and the four tables this app keeps in it.

socialchimp creates none of these. It has no models and runs no migrations,
on purpose: a library that owns a table owns your schema, your database
engine and your deployment order, and can then only serve one framework
properly. So the schema below is entirely this app's, and you can rename
every column in it without socialchimp noticing.

**Why a connection is opened per call rather than kept.** The storage class
in `storage.py` is blocking, and `sync_storage` runs each of its five
methods on whatever spare thread is free at the time. A sqlite connection
belongs to the thread that made it, so one held in a module global would be
used from the wrong thread and refused. Opening one per call costs
microseconds against a local file and is the honest answer here. A real app
on Postgres would use its pool instead, which has the same property: hand a
connection back before the call returns.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS social_connection (
    id                        TEXT PRIMARY KEY,
    platform                  TEXT NOT NULL,
    host                      TEXT,
    account_id                TEXT NOT NULL,
    account_name              TEXT NOT NULL,
    access_token              TEXT NOT NULL,
    refresh_token             TEXT,
    expires_at                TEXT,
    refresh_token_expires_at  TEXT,
    scopes                    TEXT NOT NULL,
    extra                     TEXT NOT NULL
);

-- host is '' rather than NULL for the networks that have one server,
-- because in sqlite NULL is not equal to NULL and a primary key holding one
-- would let the same row be inserted twice. `storage.py` maps '' back to
-- None on the way out, which is what socialchimp expects.
CREATE TABLE IF NOT EXISTS social_app (
    platform       TEXT NOT NULL,
    host           TEXT NOT NULL DEFAULT '',
    client_id      TEXT NOT NULL,
    client_secret  TEXT NOT NULL,
    PRIMARY KEY (platform, host)
);

-- Where a half-finished sign-in waits. Keyed by the sign-in's state, which
-- is the one value that makes the round trip out to the network and back.
-- The browser is given the state and nothing else: `remember` can hold the
-- secret half of a PKCE pair and `resume_token` can hold the person's own
-- access token, and neither belongs in a cookie.
CREATE TABLE IF NOT EXISTS login_note (
    state         TEXT PRIMARY KEY,
    platform      TEXT NOT NULL,
    host          TEXT,
    remember      TEXT NOT NULL,
    resume_token  TEXT,
    started_at    TEXT NOT NULL
);

-- Everything that happened, so a refusal is a row somebody can read later
-- rather than a message that scrolled past. socialchimp raises; deciding
-- that a failure is worth writing down is this app's decision.
CREATE TABLE IF NOT EXISTS activity (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    at             TEXT NOT NULL,
    what           TEXT NOT NULL,
    platform       TEXT NOT NULL,
    connection_id  TEXT NOT NULL,
    detail         TEXT NOT NULL,
    link           TEXT
);
"""


def set_up(database: Path) -> None:
    """Create the tables if they are not there yet.

    Called once when the app is built. A real app would put this in a
    migration of its own; the point here is only that the tables are yours.

    Args:
        database: Where the sqlite file lives.
    """
    database.parent.mkdir(parents=True, exist_ok=True)
    with opened(database) as db:
        db.executescript(SCHEMA)


@contextmanager
def opened(database: Path) -> Iterator[sqlite3.Connection]:
    """Open the database for one piece of work, then close it again.

    Committed when the block finishes without raising, rolled back when it
    does not, and closed either way.

    Args:
        database: Where the sqlite file lives.

    Yields:
        The connection, handing back rows you can read by column name.
    """
    db = sqlite3.connect(database)
    db.row_factory = sqlite3.Row
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def now() -> str:
    """The moment, written the way every timestamp in this database is.

    Returns:
        An ISO 8601 string in UTC. Times with no timezone compare wrongly
        against everything else and fail silently, which is why socialchimp
        refuses one outright.
    """
    return datetime.now(UTC).isoformat()


def write_activity(
    database: Path,
    *,
    what: str,
    platform: str,
    connection_id: str,
    detail: str,
    link: str | None = None,
) -> None:
    """Write one line of history.

    Args:
        database: Where the sqlite file lives.
        what: A short word for the kind of thing this is - `"posted"`,
            `"refused"`, `"update"`.
        platform: Which network it concerns.
        connection_id: Which connection it concerns.
        detail: What happened, in words a person can read.
        link: A link to the thing, where there is one.
    """
    with opened(database) as db:
        db.execute(
            "INSERT INTO activity (at, what, platform, connection_id, "
            "detail, link) VALUES (?, ?, ?, ?, ?, ?)",
            (now(), what, platform, connection_id, detail, link),
        )


@dataclass(frozen=True, slots=True)
class Happening:
    """One line of history, ready to show on a page.

    Attributes:
        at: When it happened, as it was written down.
        what: A short word for the kind of thing it is.
        platform: Which network it concerns.
        connection_id: Which connection it concerns.
        detail: What happened, in words.
        link: A link to the thing, where there is one.
    """

    at: str
    what: str
    platform: str
    connection_id: str
    detail: str
    link: str | None


def recent_activity(database: Path, *, limit: int = 20) -> list[Happening]:
    """Read the last few things that happened.

    Args:
        database: Where the sqlite file lives.
        limit: How many rows to read.

    Returns:
        The newest first.
    """
    with opened(database) as db:
        rows = db.execute(
            "SELECT at, what, platform, connection_id, detail, link "
            "FROM activity ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    return [
        Happening(
            at=str(row["at"]),
            what=str(row["what"]),
            platform=str(row["platform"]),
            connection_id=str(row["connection_id"]),
            detail=str(row["detail"]),
            link=None if row["link"] is None else str(row["link"]),
        )
        for row in rows
    ]
