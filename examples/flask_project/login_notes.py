"""Where a half-finished sign-in waits for the person to come back.

Signing in is two requests to this app with a trip through somebody else's
website in between, and on Facebook, Instagram and YouTube it is three. The
first request is handed things the later ones need - the secret half of a
PKCE pair, which Mastodon server the person named, and the resume token that
`ChooseAccount` hands out - and socialchimp cannot keep any of it for you.
The person can be sent away by one web worker and come back to another, so
anything held in a module variable works on your laptop and fails the day
you run a second process.

Everything is filed under the sign-in's `state`, because that is the one
value that makes the round trip out to the network and back.

**Why a table rather than the Flask session.** Flask's session is a signed
cookie: signed means nobody can change it, not that nobody can read it, and
its whole contents travel to the browser and back on every request. On
Facebook the resume token carries the person's own access token, because
Facebook's code can only be swapped once and that swap happens before they
have picked a Page. Putting it in a cookie would be handing it out. So the
browser gets the state and nothing else, and everything the state stands for
stays here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from socialchimp import RawData

from .db import now, opened

GIVE_UP_AFTER = timedelta(hours=1)
"""How long a half-finished sign-in is worth keeping.

Long enough that somebody can go and find their password, short enough that
a table of abandoned sign-ins does not grow forever.
"""


@dataclass(frozen=True, slots=True)
class LoginNote:
    """What one sign-in wrote down for the requests that come after it.

    Attributes:
        state: The value that makes the round trip through the network.
        platform: Which network the sign-in is with.
        host: Which server, for Mastodon.
        remember: What `SendToNetwork.remember` held. Hand it back to
            `finish_login` and to `choose` unchanged.
        resume_token: What `ChooseAccount` handed out, once the network has
            paused to ask which account. `None` before then. A secret.
    """

    state: str
    platform: str
    host: str | None
    remember: RawData
    resume_token: str | None


class LoginNotes:
    """The `login_note` table, read and written the ordinary blocking way.

    These are the app's own routes, so nothing here has to be `async`. If
    you use the ready-made blueprint in `socialchimp.contrib.flask` instead,
    it wants a `LoginMemory` - the same idea with three `async` methods
    named `keep`, `look_up` and `forget` - and you would wrap this the way
    `sync_storage` wraps the storage class.
    """

    def __init__(self, database: Path) -> None:
        """Point the notes at a sqlite file.

        Args:
            database: Where the file lives.
        """
        self.database = database

    def keep(
        self,
        state: str,
        *,
        platform: str,
        host: str | None,
        remember: RawData,
    ) -> None:
        """Write down what the rest of this sign-in will need.

        Args:
            state: The sign-in's state, which is the key.
            platform: Which network.
            host: Which server, for Mastodon.
            remember: What `SendToNetwork.remember` held.
        """
        with opened(self.database) as db:
            db.execute(
                "INSERT OR REPLACE INTO login_note "
                "(state, platform, host, remember, resume_token, started_at) "
                "VALUES (?, ?, ?, ?, "
                "(SELECT resume_token FROM login_note WHERE state = ?), ?)",
                (state, platform, host, json.dumps(remember), state, now()),
            )

    def keep_resume_token(self, state: str, resume_token: str) -> None:
        """Add the resume token to a sign-in that paused to ask.

        Args:
            state: The sign-in's state.
            resume_token: What `ChooseAccount` handed out. Treated as a
                secret: it never goes near the browser, a URL or a log.
        """
        with opened(self.database) as db:
            db.execute(
                "UPDATE login_note SET resume_token = ? WHERE state = ?",
                (resume_token, state),
            )

    def look_up(self, state: str) -> LoginNote | None:
        """Read back what was kept for one sign-in.

        Args:
            state: The sign-in's state.

        Returns:
            The note, or `None` if there is nothing under that state or it
            is older than `GIVE_UP_AFTER`. Both look the same to the person
            - their sign-in has expired and they should start again.
        """
        with opened(self.database) as db:
            row = db.execute(
                "SELECT state, platform, host, remember, resume_token, "
                "started_at FROM login_note WHERE state = ?",
                (state,),
            ).fetchone()

        if row is None:
            return None

        started = datetime.fromisoformat(str(row["started_at"]))
        if datetime.now(UTC) - started > GIVE_UP_AFTER:
            self.forget(state)
            return None

        remember: Any = json.loads(str(row["remember"]))
        return LoginNote(
            state=str(row["state"]),
            platform=str(row["platform"]),
            host=None if row["host"] is None else str(row["host"]),
            remember=remember if isinstance(remember, dict) else {},
            resume_token=(
                None if row["resume_token"] is None else str(row["resume_token"])
            ),
        )

    def forget(self, state: str) -> None:
        """Throw one sign-in's notes away, quietly if there are none.

        Called the moment a sign-in finishes. What is in here is secret and
        of no further use, so there is no reason to keep it.

        Args:
            state: The sign-in's state.
        """
        with opened(self.database) as db:
            db.execute("DELETE FROM login_note WHERE state = ?", (state,))
