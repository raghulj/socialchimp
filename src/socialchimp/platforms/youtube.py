"""YouTube: the network where every post is a file, and the file can be huge.

Everywhere else socialchimp goes, a post is a bit of text with maybe a
picture stapled to it, and the network is finished with it by the time the
request comes back. YouTube is neither of those things.

**A post is a video.** There is no text-only post here at all. YouTube's
community posts are text, and they are not in its API - not read, not write,
not at any access level. So `Feature.POST_TEXT` is off, and a post with no
video is refused rather than turned into something else.

**The video is uploaded in pieces.** A video can be gigabytes. YouTube takes
it a piece at a time, tells you after every piece how much of it actually
arrived, and lets you carry on from there if something drops. socialchimp
does all of that for you, reads a file from disk one piece at a time so a
four-gigabyte video does not need four gigabytes of memory, and carries on
from where YouTube says it got to rather than assuming.

**YouTube keeps working after it says yes.** Taking the bytes is not
publishing. Encoding happens afterwards and can take minutes or hours, so
`publish()` hands back `PostState.PROCESSING`, not `DONE`. Ask
`check_state()` later to find out how it went.

## Before any of this works

There is no `create_app` here, because there is nothing to automate. A person
has to:

1. Make a project at https://console.cloud.google.com
2. Turn on the **YouTube Data API v3** for it
3. Create an **OAuth client** and add your redirect address to it
4. Ask Google to **review** the app, because uploading video counts as a
   sensitive permission. Until that review passes, only accounts you add as
   test users can sign in.

Then hand the client id and secret to socialchimp as `AppCredentials`.

## Signing someone in

Two steps, like Mastodon: `start_login` gives you an address to send the
person to, and `finish_login` swaps the code they come back with for a token.
The address carries the hash of a secret (PKCE); the secret itself comes back
to you in `SendToNetwork.remember`, and you hand it to `finish_login`.

The address also carries `access_type=offline` and `prompt=consent`. Both are
there on purpose. **Leave either one out and Google hands back no refresh
token at all**, the access token dies an hour later, and the person has to
sign in again - which is exactly the sort of thing that works on your laptop
in the morning and starts logging people out by lunchtime.

## Then it asks which channel

One Google account can own several YouTube channels. So `finish_login`
answers with `ChooseAccount` rather than a finished connection, you show the
list, and `resume_login` finishes the job with the one they picked. It asks
even when there is only one channel, because a person can add a second
tomorrow and an app that only handled the one-channel case breaks that day.

The `resume_token` in between holds the tokens Google just issued. It has to:
the code Google sent back can only be swapped once, so there is nothing left
to swap by the time the person has picked. Keep it with that person's session
on your server, and treat it like the token it is - not in a URL, not in a
hidden form field.

## Tokens

An access token lasts about an hour. The refresh token lasts until it is
revoked, and **Google does not replace it** - a renewal answers with a new
access token and nothing else. That is the opposite of Bluesky, so the
refresh token we already hold is carried across rather than read out of the
reply.

Renewing needs your app's client id and secret. They arrive as an argument,
the same way they arrive for a sign-in: `SocialChimp` reads them out of your
storage and hands them to `refresh`. Save them once with `Storage.save_app`
and there is nothing else to do.

## Shorts are not a different kind of post

There is no Shorts endpoint, no Shorts flag, and nothing here that can make a
video into a Short. **YouTube decides.** A video becomes a Short by being
vertical (taller than it is wide) and under about three minutes. Both of
those are properties of the file you upload, decided long before socialchimp
sees it.

So there is deliberately no `shorts=True` option, because it would be a lie.
Upload a vertical video under three minutes and you get a Short. Putting
`#Shorts` in the title or description is a convention people follow to help
YouTube along; it is not what makes the decision.

## What a post can carry

    Post(
        text="What happens in the video",   # this is the description
        media=(Media.from_file("clip.mp4"),),
        options={
            "title": "My video",            # required
            "made_for_kids": False,         # required
            "privacy_status": "unlisted",   # private, unlisted, public
            "category_id": "22",
            "tags": ["python", "async"],
            "notify_subscribers": False,
        },
    )

`Post.text` is the **description**, and `title` is a separate setting, which
catches people out: a video with no title is refused by YouTube, so it is
refused here first, by name.

`made_for_kids` is required because Google requires it. Getting it wrong has
consequences for the channel, so there is no guess here and no default.

**A video with no `privacy_status` is uploaded as private.** Making somebody's
video public by accident cannot be undone, so the quiet default is the safe
one. Say `privacy_status="public"` when you mean it.

## Quota is not a rate limit

A project gets 10,000 units a day and an upload costs about 1,600, so roughly
six uploads a day on the default allowance. Going over gives `quotaExceeded`,
which socialchimp raises as `RateLimitError` - but with a message saying
plainly that waiting thirty seconds will not help. It resets at midnight
Pacific time, and nothing before then changes the answer.

## What is not here

- **No `delete_post`.** Deleting a video needs the `youtube.force-ssl`
  permission, which is a wider one than uploading and gets a harder look in
  Google's review. It is not worth asking every app to request it for
  something most never do.
- **No pushed updates.** YouTube has WebSub, and it announces new uploads on
  a channel - not comments, not likes, nothing else. Comments are what apps
  actually want, so socialchimp reads them on a timer through
  `fetch_updates` and `Feature.PUSH_UPDATES` stays off.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import httpx

from socialchimp.errors import (
    AuthError,
    ConfigError,
    InvalidPostError,
    NotAllowedError,
    NotFoundError,
    PlatformError,
    RateLimitError,
    TokenExpiredError,
)
from socialchimp.events import Update
from socialchimp.features import (
    Feature,
    Limits,
    TextCount,
    check_option_names,
    check_post,
)
from socialchimp.http import HttpClient, error_from_response, read_body
from socialchimp.models import (
    Connection,
    Media,
    Post,
    PostResult,
    PostState,
    RawData,
    Token,
)
from socialchimp.platform import (
    AccountChoice,
    ChooseAccount,
    Finished,
    LoginRequest,
    SendToNetwork,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from socialchimp.errors import SocialChimpError
    from socialchimp.http import Retries
    from socialchimp.models import AppCredentials

__all__ = ["YouTubePlatform", "youtube_errors"]

PLATFORM_NAME: Final = "youtube"

API_URL: Final = "https://www.googleapis.com/youtube/v3"
"""Where everything except the upload itself is sent."""

UPLOAD_URL: Final = "https://www.googleapis.com/upload/youtube/v3/videos"
"""Where an upload starts. A different address from the rest of the API."""

SIGN_IN_URL: Final = "https://accounts.google.com/o/oauth2/v2/auth"
# Where a code, or a refresh token, is swapped for an access token. The
# linter reads any name ending in TOKEN as a secret; this one is a public
# address, the same for every app in the world.
TOKEN_URL: Final = "https://oauth2.googleapis.com/token"  # noqa: S105

CONSOLE_URL: Final = "https://console.cloud.google.com/apis/credentials"
"""Where a person creates the OAuth client this all needs."""

DEFAULT_SCOPES: Final = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
)
"""Enough to upload a video and to read the channel's own comments.

Both are permissions Google reviews before anyone outside your own project
can grant them. Ask for fewer if your app only ever uploads.
"""

PRIVACY_STATUSES: Final = ("private", "unlisted", "public")
"""Who can see a video."""

POST_OPTIONS: Final = (
    "title",
    "privacy_status",
    "category_id",
    "tags",
    "made_for_kids",
    "notify_subscribers",
)
"""The settings `Post.options` accepts here. Anything else is refused."""

CHUNK_MULTIPLE: Final = 256 * 1024
"""YouTube takes pieces in multiples of this, except the last one."""

DEFAULT_CHUNK_BYTES: Final = 8 * 1024 * 1024
"""How much of a video to send at a time. Big enough to be quick, small
enough that a dropped connection costs seconds rather than minutes."""

MAX_TITLE_CHARACTERS: Final = 100
MAX_DESCRIPTION_BYTES: Final = 5000

# A quarter of a terabyte, which is YouTube's largest file today.
MAX_VIDEO_BYTES: Final = 256 * 1024**3

# One video per post. There is no such thing as a YouTube post with two.
MAX_VIDEOS_PER_POST: Final = 1

# Long enough that nobody can guess one, short enough to sit in a URL.
_STATE_BYTES: Final = 24
_VERIFIER_BYTES: Final = 48

# What YouTube says about a video it has finished with, and what we call it.
# Anything not listed here is treated as "still working on it", because a
# state we have never heard of is far more likely to be a new step in the
# middle than a new kind of failure.
_OUR_STATE_FOR: Final = {
    "processed": PostState.DONE,
    "uploaded": PostState.PROCESSING,
    "failed": PostState.FAILED,
    "rejected": PostState.FAILED,
    "deleted": PostState.FAILED,
}


def _text(reply: RawData, key: str, when: str) -> str:
    """Read a value YouTube always sends, and complain plainly if it did not.

    Args:
        reply: What YouTube answered.
        key: The field we need.
        when: What we had asked it to do, for the message.

    Returns:
        The value.

    Raises:
        PlatformError: If the field is missing or empty. The whole reply is
            kept on the error so you can see what did arrive.
    """
    value = reply.get(key)
    if isinstance(value, str) and value:
        return value

    message = (
        f"YouTube left {key!r} out of its reply when we asked it to {when}. "
        f"That should not happen. The whole reply is on this error."
    )
    raise PlatformError(message, platform=PLATFORM_NAME, raw=reply)


def _items_in(reply: RawData) -> list[RawData]:
    """Pull the list of things out of one of Google's replies.

    Every list endpoint answers the same shape: the results under `items`.

    Args:
        reply: What Google answered.

    Returns:
        The items, or an empty list when there are none.
    """
    found = reply.get("items")
    if not isinstance(found, list):
        return []
    return [item for item in found if isinstance(item, dict)]


def _moment(text: str) -> datetime | None:
    """Read a time Google wrote, such as `"2026-08-31T10:00:00Z"`.

    Args:
        text: The time as it arrived.

    Returns:
        The moment, always with a timezone, or `None` if it cannot be read.
    """
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def _challenge_for(verifier: str) -> str:
    """Hash the secret we keep, so only the hash travels to Google.

    Args:
        verifier: The secret made at the start of a login.

    Returns:
        The hash, written the way the PKCE rules ask for.
    """
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _pack(kept: RawData) -> str:
    """Turn what a paused login needs to remember into one piece of text.

    Args:
        kept: The values to carry through the pause.

    Returns:
        Text that `_unpack` can read back.
    """
    written = json.dumps(kept, sort_keys=True).encode()
    return base64.urlsafe_b64encode(written).decode().rstrip("=")


def _unpack(packed: str) -> RawData:
    """Read back what `_pack` wrote.

    Args:
        packed: The `resume_token` handed back to us.

    Returns:
        The values a paused login was carrying.

    Raises:
        AuthError: If it cannot be read. Either it was changed on the way
            round, or it belongs to a different login.
    """
    padded = packed + "=" * (-len(packed) % 4)
    try:
        parsed = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except ValueError:
        parsed = None

    if not isinstance(parsed, dict):
        message = (
            "This resume_token could not be read, so the login cannot be "
            "carried on. Hand back exactly the value from ChooseAccount, and "
            "start a new login if it has been lost."
        )
        raise AuthError(message, platform=PLATFORM_NAME)
    return parsed


def _reason_in(body: RawData) -> str:
    """Pull Google's short name for what went wrong out of a refusal.

    Google wraps every error the same way: an `error` object with a list of
    `errors` inside it, each carrying a `reason` such as `"quotaExceeded"`.
    The status alone is not enough - 403 covers both "you have no permission"
    and "you have used up the day's quota", which want very different advice.

    Args:
        body: The refusal, already read into a dictionary.

    Returns:
        The reason, or an empty string when there is not one.
    """
    problem = body.get("error")
    if not isinstance(problem, dict):
        return ""

    listed = problem.get("errors")
    if isinstance(listed, list):
        for item in listed:
            if isinstance(item, dict):
                reason = item.get("reason")
                if isinstance(reason, str) and reason:
                    return reason

    status = problem.get("status")
    return status if isinstance(status, str) else ""


def _said_in(body: RawData) -> str:
    """Pull Google's own sentence out of a refusal, ready to append.

    Args:
        body: The refusal, already read into a dictionary.

    Returns:
        `" It said: ..."`, or an empty string when it said nothing.
    """
    problem = body.get("error")
    if isinstance(problem, dict):
        said = problem.get("message")
        if isinstance(said, str) and said:
            return f" It said: {said}"
    return ""


def youtube_errors(response: httpx.Response) -> SocialChimpError:
    """Turn an unhappy reply from YouTube into a socialchimp error.

    Google's status codes are not enough on their own, so this reads the
    short reason it puts in the body. Five of those are worth naming:

    - **quotaExceeded** is the one that catches everyone. It arrives as a
      403, which reads like a permission problem, and it becomes a
      `RateLimitError` here - but with a message saying so in plain words,
      because the usual advice for a rate limit is wrong for this one. There
      is nothing to wait a few seconds for. It is a daily allowance.
    - **uploadLimitExceeded** is the same idea for videos rather than
      requests: the channel has uploaded as many as it may today.
    - **authError** is a token Google will not take, whatever the status.
    - **videoNotFound** is a video that is not there.
    - **invalidTitle** is a title YouTube will not have, which is a problem
      with the post rather than a mystery.

    Everything else falls through to the shared mapping: 401 is an
    `AuthError`, 403 a `NotAllowedError`, 404 a `NotFoundError`, 429 a
    `RateLimitError`.

    Args:
        response: The reply to turn into an error.

    Returns:
        The error to raise.
    """
    body = read_body(response)
    reason = _reason_in(body)
    said = _said_in(body)

    if reason == "quotaExceeded":
        message = (
            f"YouTube's daily quota for this Google Cloud project is used up "
            f"(quotaExceeded). This is a daily allowance, not a request to "
            f"slow down: a project gets 10,000 units a day and an upload "
            f"costs about 1,600, so roughly six uploads. It starts again at "
            f"midnight Pacific time, and trying again in a few seconds only "
            f"spends what is left. Ask for more quota in the Google Cloud "
            f"console, or wait for the reset.{said}"
        )
        # No `retry_after`: Google does not say when, and putting a number
        # here would have callers retrying inside the same day and burning
        # what is left of it.
        return RateLimitError(message, platform=PLATFORM_NAME, raw=body)

    if reason == "uploadLimitExceeded":
        message = (
            f"This channel has uploaded as many videos as YouTube allows it "
            f"today (uploadLimitExceeded). Like the quota, this is a daily "
            f"allowance rather than a request to slow down - the count "
            f"starts again tomorrow.{said}"
        )
        return RateLimitError(message, platform=PLATFORM_NAME, raw=body)

    if reason == "authError":
        message = (
            f"YouTube would not accept our sign-in (authError). The token "
            f"has run out or been taken away; renewing it, or asking the "
            f"person to connect their channel again, is what fixes it.{said}"
        )
        return AuthError(message, platform=PLATFORM_NAME, raw=body)

    if reason == "forbidden":
        message = (
            f"YouTube will not let this channel do that (forbidden). It is "
            f"usually a permission that was never asked for, or an app whose "
            f"review Google has not finished.{said}"
        )
        return NotAllowedError(message, platform=PLATFORM_NAME, raw=body)

    if reason == "videoNotFound":
        message = f"YouTube has no such video (videoNotFound).{said}"
        return NotFoundError(message, platform=PLATFORM_NAME, raw=body)

    if reason == "invalidTitle":
        message = (
            f"YouTube would not accept this video's title (invalidTitle). A "
            f"title has to be there, be at most {MAX_TITLE_CHARACTERS} "
            f"characters, and contain no < or > characters.{said}"
        )
        return InvalidPostError(message, platform=PLATFORM_NAME, raw=body)

    return error_from_response(response, platform=PLATFORM_NAME)


def _app_or_refuse(app: AppCredentials | None, what: str) -> AppCredentials:
    """Insist on your app's credentials before going any further.

    Args:
        app: The credentials that arrived, which may be none at all.
        what: The thing we were trying to do, for the message.

    Returns:
        The credentials.

    Raises:
        ConfigError: If there are none, saying where to get some.
    """
    if app is None:
        raise ConfigError(_no_credentials(what))
    return app


def _no_credentials(what: str) -> str:
    """Say what to do when there are no credentials to work with.

    Args:
        what: The thing we were trying to do, for the first sentence.

    Returns:
        A message naming every step somebody has to take by hand.
    """
    return (
        f"YouTube needs your app's client id and secret to {what}, and none "
        f"were given. socialchimp cannot make them for you: somebody has to "
        f"create a project at {CONSOLE_URL}, turn on the YouTube Data API "
        f"v3, create an OAuth client, and add your redirect address to it. "
        f"Uploading video is a sensitive permission, so Google will also "
        f"review the app before anyone outside your own test users can sign "
        f"in. Save what the console gives you with Storage.save_app, and "
        f"socialchimp hands them to every sign-in and every renewal."
    )


def _check_state(request: LoginRequest, callback: Mapping[str, str]) -> None:
    """Check the value that came back is the one we sent.

    This is what stops somebody handing your app a login they started
    themselves and having it saved against one of your users.

    Args:
        request: The request used to start the login.
        callback: The query values Google sent back.

    Raises:
        AuthError: If both sides have a state and they are different.
    """
    returned = callback.get("state", "")
    if request.state is not None and returned and returned != request.state:
        message = (
            "The state Google sent back did not match the one we sent. This "
            "login did not start here, so nothing has been saved. Start a "
            "new one."
        )
        raise AuthError(message, platform=PLATFORM_NAME)


def _code_from(callback: Mapping[str, str]) -> str:
    """Pull the login code out of what Google sent back.

    Args:
        callback: The query values Google sent back.

    Returns:
        The code to swap for a token.

    Raises:
        AuthError: If the person said no, or if there is no code.
    """
    refused = callback.get("error")
    if refused:
        said = callback.get("error_description", "")
        detail = f" It said: {said}" if said else ""
        message = (
            f"Google did not sign this person in ({refused}). Usually they "
            f"pressed cancel on the approval page.{detail}"
        )
        raise AuthError(message, platform=PLATFORM_NAME)

    code = callback.get("code")
    if not code:
        message = (
            "Google sent no code back, so there is nothing to swap for a "
            "token. Check you are passing the whole query string from your "
            "redirect address."
        )
        raise AuthError(message, platform=PLATFORM_NAME)
    return code


def _verifier_from(remember: RawData | None) -> str:
    """Read the secret `start_login` made back out of what your app kept.

    Args:
        remember: What `start_login` put in `SendToNetwork.remember`.

    Returns:
        The secret to send with the code.

    Raises:
        AuthError: If it did not come back. Without it Google cannot tell
            that this is the same sign-in it started, and will refuse the
            code - so saying it here is clearer than letting Google say it
            in its own words.
    """
    verifier = (remember or {}).get("code_verifier")
    if not isinstance(verifier, str) or not verifier:
        message = (
            "This sign-in cannot be finished because the secret made at the "
            "start did not come back. Pass SendToNetwork.remember to "
            "finish_login as `remember`. Keep it with that person's session "
            "rather than in memory: they may be sent away by one web worker "
            "and come back to another."
        )
        raise AuthError(message, platform=PLATFORM_NAME)
    return verifier


def _is_googles_own_fault(refused: SocialChimpError) -> bool:
    """Say whether a refusal was Google struggling rather than a dead token.

    Args:
        refused: The error the token endpoint gave us.

    Returns:
        True for a 500 and for a reply we could not read at all. Both are
        worth trying again; neither means the person has to sign in again,
        and telling an app to delete a connection over a bad five minutes at
        Google is the sort of quiet damage this library exists to avoid.
    """
    if not isinstance(refused, PlatformError):
        return False
    return (
        refused.status_code is None
        or refused.status_code >= httpx.codes.INTERNAL_SERVER_ERROR
    )


def _how_big(video: Media) -> int:
    """Find out how many bytes a video is, without reading any of them.

    Nothing here ever holds the whole file. `Media.size` reads the size off
    disk, and `Media.piece` hands out one piece at a time, so a four
    gigabyte video costs one piece of memory rather than four gigabytes.

    Args:
        video: The video attached to the post.

    Returns:
        How many bytes there are altogether.

    Raises:
        InvalidPostError: If all we have is a link to the file. `Media.size`
            says `None` for one, because finding out would mean downloading
            it - and YouTube will not fetch it either.
    """
    total = video.size
    if total is None:
        message = (
            f"YouTube will not fetch {video.url!r} for you - it only takes "
            f"files sent to it, a piece at a time. Download the file first, "
            f"then use Media.from_file, which reads it off disk as it goes "
            f"rather than holding all of it in memory."
        )
        raise InvalidPostError(message, platform=PLATFORM_NAME)
    return total


# What `check_post` adds to its own "this network has no text-only post"
# message here. The API not having community posts in it is the thing
# everybody argues back with, so it is worth saying before they go looking.
WORDS_ALONE_ADVICE: Final = (
    "Media.from_file('clip.mp4') will do it. Community posts are words, but "
    "they are not in YouTube's API at all."
)


def _the_video(post: Post) -> Media:
    """Find the one video a YouTube post is made of.

    Args:
        post: The post about to be sent.

    Returns:
        The video to upload.
    """
    # By the time this runs, `check_post` has already turned away pictures -
    # YouTube takes none - any post carrying more than one video, and a post
    # with nothing attached at all. So what is left is exactly one video.
    return post.media[0]


def _checked_title(options: RawData, allowed: int | None) -> str:
    """Check the one setting YouTube will not do without.

    Args:
        options: What was put in `Post.options`.
        allowed: The longest title this network takes, from
            `Limits.max_title_length`. `None` means we do not know, and
            nothing is checked - the same as every other unset limit.

    Returns:
        The title to send.

    Raises:
        InvalidPostError: If there is no title, or it is too long. YouTube
            refuses a video with no title, and `Post.text` is the
            description here rather than the title, which is the part people
            miss.
    """
    title = options.get("title")
    if not isinstance(title, str) or not title:
        message = (
            "This post has no title, and YouTube refuses a video without "
            "one. Post.text is the video's description here, so the title "
            "is its own setting: Post(..., options={'title': 'My video'})."
        )
        raise InvalidPostError(message, platform=PLATFORM_NAME)

    if allowed is not None and len(title) > allowed:
        message = (
            f"This title is {len(title)} characters but YouTube allows at "
            f"most {allowed}. The rest of what you want to say belongs in "
            f"Post.text, which becomes the description and has room for "
            f"{MAX_DESCRIPTION_BYTES} bytes."
        )
        raise InvalidPostError(message, platform=PLATFORM_NAME)
    return title


def _checked_flag(options: RawData, key: str, why: str) -> bool | None:
    """Read a setting that has to be true or false, if it is there at all.

    Args:
        options: What was put in `Post.options`.
        key: Which setting to read.
        why: What the setting decides, for the message.

    Returns:
        The answer, or `None` when the setting was left out.

    Raises:
        InvalidPostError: If it is there but is not true or false.
    """
    if key not in options:
        return None

    value = options[key]
    if not isinstance(value, bool):
        message = f"{key} is {value!r}, but it has to be True or False. It {why}."
        raise InvalidPostError(message, platform=PLATFORM_NAME)
    return value


def _checked_tags(options: RawData) -> list[str] | None:
    """Read the list of tags, if there is one.

    Args:
        options: What was put in `Post.options`.

    Returns:
        The tags, or `None` when none were given.

    Raises:
        InvalidPostError: If it is not a list of words. A single string is
            the usual mistake, and YouTube would take it as a list of
            letters rather than saying anything.
    """
    if "tags" not in options:
        return None

    value = options["tags"]
    if not isinstance(value, list | tuple) or not all(
        isinstance(one, str) and one for one in value
    ):
        message = (
            f"tags is {value!r}, but it has to be a list of words: "
            f"['python', 'async']. One string on its own is read as a list "
            f"of letters. All of them together may be at most 500 "
            f"characters."
        )
        raise InvalidPostError(message, platform=PLATFORM_NAME)
    return [str(one) for one in value]


def _checked_privacy(options: RawData, post: Post) -> str:
    """Work out who should be able to see this video.

    Args:
        options: What was put in `Post.options`.
        post: The post about to be sent, for its publishing time.

    Returns:
        The privacy status to send.

    Raises:
        InvalidPostError: If it is not one YouTube knows, or if a scheduled
            video was asked to be public straight away.
    """
    # Nothing said means private. Making somebody's video public by accident
    # cannot be undone, so the quiet answer is the careful one.
    asked = options.get("privacy_status", "private")
    if asked not in PRIVACY_STATUSES:
        message = (
            f"privacy_status is {asked!r}, which YouTube does not know. It "
            f"accepts: {', '.join(PRIVACY_STATUSES)}."
        )
        raise InvalidPostError(message, platform=PLATFORM_NAME)

    if post.publish_at is not None and asked != "private":
        message = (
            f"This video is set to go out at "
            f"{post.publish_at.isoformat()} but privacy_status is {asked!r}. "
            f"YouTube's own rule is that a video with a publishing time must "
            f"be private until then - anything else is visible straight "
            f"away, which is the opposite of scheduling it. Leave "
            f"privacy_status out, or set it to 'private'; YouTube makes it "
            f"public at the moment you gave."
        )
        raise InvalidPostError(message, platform=PLATFORM_NAME)
    return str(asked)


def _checked_options(options: RawData) -> None:
    """Refuse a setting YouTube has never heard of, before anything is sent.

    Args:
        options: What was put in `Post.options`.

    Raises:
        InvalidPostError: If a setting is not one of ours. A typo costs no
            request and no part of the day's quota.
    """
    # Checked ahead of the shared name check, because "YouTube does not
    # know publish_at" would be a lie - it can schedule, it just wants the
    # time on the post rather than in here.
    if "publish_at" in options:
        message = (
            "YouTube can schedule, and socialchimp carries the time "
            "on Post.publish_at rather than in options, so that every "
            "network is asked the same way. Use "
            "Post(publish_at=when, ...) instead."
        )
        raise InvalidPostError(message, platform=PLATFORM_NAME)

    check_option_names(options, platform=PLATFORM_NAME, allowed=POST_OPTIONS)


def _metadata_for(post: Post, limits: Limits) -> tuple[RawData, bool | None]:
    """Build the JSON that describes a video, and check every part of it.

    Args:
        post: The post about to be sent.
        limits: What YouTube allows. The title is checked against
            `max_title_length` here rather than by `check_post`, because a
            title is not something every network has - it lives in
            `Post.options`, which only this file understands.

    Returns:
        What to send as the body, and whether to tell subscribers - which
        is a query value rather than part of the body.

    Raises:
        InvalidPostError: If anything in `Post.options` is wrong. All of
            this happens before a single request.
    """
    options = post.options
    _checked_options(options)

    snippet: RawData = {
        "title": _checked_title(options, limits.max_title_length),
        # `Post.text` is the description everywhere on YouTube. The title is
        # separate, which is the thing people trip over.
        "description": post.text,
    }

    tags = _checked_tags(options)
    if tags is not None:
        snippet["tags"] = tags

    if "category_id" in options:
        category = options["category_id"]
        if not isinstance(category, str) or not category:
            message = (
                f"category_id is {category!r}, but it has to be text: "
                f"'22' for People & Blogs, '28' for Science & Technology. "
                f"videoCategories.list gives the whole list for a country."
            )
            raise InvalidPostError(message, platform=PLATFORM_NAME)
        snippet["categoryId"] = category

    made_for_kids = _checked_flag(
        options,
        "made_for_kids",
        "says whether this video is made for children",
    )
    if made_for_kids is None:
        message = (
            "This post does not say whether the video is made for children, "
            "and Google requires an answer for every upload. Set "
            "options={'made_for_kids': False} - or True - and mean it: it "
            "changes what YouTube allows on the video, and getting it wrong "
            "has consequences for the channel. socialchimp will not guess."
        )
        raise InvalidPostError(message, platform=PLATFORM_NAME)

    status: RawData = {
        "privacyStatus": _checked_privacy(options, post),
        "selfDeclaredMadeForKids": made_for_kids,
    }
    if post.publish_at is not None:
        status["publishAt"] = post.publish_at.isoformat()

    notify = _checked_flag(
        options,
        "notify_subscribers",
        "decides whether the channel's subscribers are told about this video",
    )
    return {"snippet": snippet, "status": status}, notify


def _arrived_so_far(response: httpx.Response) -> int:
    """Read how much of the video YouTube says it has, from a 308.

    YouTube answers `Range: bytes=0-262143` to mean "I have everything up to
    and including byte 262143". No `Range` header at all means none of it
    arrived, which is a real answer rather than a missing one.

    Args:
        response: The 308 reply to read.

    Returns:
        The byte to carry on from.
    """
    header = response.headers.get("range")
    if header is None:
        return 0
    try:
        last = int(header.rsplit("-", 1)[-1])
    except ValueError:
        return 0
    return last + 1


class YouTubePlatform:
    """Everything socialchimp does with YouTube.

    Signing people in, picking a channel, uploading a video in pieces,
    checking what YouTube did with it afterwards, and reading the comments.

        youtube = YouTubePlatform()

    It holds nothing belonging to one account and nothing belonging to your
    app. Your client id and secret arrive as an argument every time they are
    needed - on the `LoginRequest` for a sign-in, and on `refresh` for a
    renewal - so one of these serves every account and every app.

    Attributes:
        name: `"youtube"`.
        features: What YouTube can do. Notably `Feature.POST_TEXT` is
            missing, because YouTube has no text-only post at all.
    """

    name: str = PLATFORM_NAME

    features: Feature = Feature.POST_VIDEO | Feature.SCHEDULE | Feature.READ_POSTS

    def __init__(
        self,
        *,
        timeout: float = 300.0,
        retries: Retries | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        chunk_bytes: int = DEFAULT_CHUNK_BYTES,
        resends_allowed: int = 3,
        updates_per_check: int = 50,
    ) -> None:
        """Set YouTube up for one app.

        Args:
            timeout: Seconds to wait for a reply. Five minutes by default,
                because a single piece of a video takes far longer to send
                than an ordinary request.
            retries: How many times to try again after a hiccup. Left out,
                the shared default is used.
            transport: Where requests actually go. Leave it out for ordinary
                calls; pass your own to send them somewhere else.
            chunk_bytes: How much of a video to send at a time. YouTube
                takes multiples of 256 KB.
            resends_allowed: How many times to send the same piece again
                when YouTube says none of it arrived, before giving up.
            updates_per_check: How many comment threads to read at a time.
                YouTube allows up to 100.

        Raises:
            ConfigError: If `chunk_bytes` is not a multiple of 256 KB.
                YouTube refuses anything else, and it refuses it halfway
                through an upload rather than at the start.
        """
        if chunk_bytes <= 0 or chunk_bytes % CHUNK_MULTIPLE:
            message = (
                f"chunk_bytes is {chunk_bytes}, but YouTube only takes "
                f"pieces that are a multiple of 256 KB ({CHUNK_MULTIPLE} "
                f"bytes), apart from the last one. Try "
                f"{DEFAULT_CHUNK_BYTES} for 8 MB at a time."
            )
            raise ConfigError(message)

        self._timeout = timeout
        self._retries = retries
        self._transport = transport
        self._chunk_bytes = chunk_bytes
        self._resends_allowed = resends_allowed
        self._updates_per_check = updates_per_check

    def _client(self, base_url: str = "", token: str | None = None) -> HttpClient:
        """Make a client pointed at one of Google's addresses.

        YouTube is spread over three of them - `accounts.google.com` to sign
        in, `oauth2.googleapis.com` for tokens, `www.googleapis.com` for the
        API and for uploads - and an upload then moves to a fourth address
        that YouTube makes up on the spot. So a client here is built with no
        address of its own unless the caller names one, and the whole
        address goes on each request.

        Args:
            base_url: What to join paths onto, for the ordinary API calls.
                Left out, every request carries its whole address.
            token: The account's token, for anything that needs one.

        Returns:
            A client. Use it in an `async with` block so it closes itself.
        """
        headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
        return HttpClient(
            base_url,
            platform=PLATFORM_NAME,
            headers=headers,
            timeout=self._timeout,
            transport=self._transport,
            retries=self._retries,
            errors=youtube_errors,
        )

    def api_base(self, connection: Connection) -> str:
        """Return where YouTube's API lives.

        The same address for everybody. Uploading a video goes somewhere
        else, and `publish` handles that itself rather than bending this.

        Args:
            connection: The account we are about to act as. Not used.

        Returns:
            `"https://www.googleapis.com/youtube/v3"`.
        """
        return API_URL

    def auth_headers(self, connection: Connection) -> Mapping[str, str]:
        """Return the header that proves we may act as this channel.

        Args:
            connection: The account we are acting as.

        Returns:
            Google's `Authorization` header.
        """
        return {"Authorization": f"Bearer {connection.token.access_token}"}

    async def limits(self, connection: Connection) -> Limits:
        """Return what YouTube allows.

        Nothing is asked of YouTube here, for two reasons. None of these
        numbers changes from one channel to the next, and every request
        against the API costs part of a daily allowance that an upload has
        already eaten most of - so spending one to be told a number that is
        written in the documentation would be a poor trade.

        Args:
            connection: The account to ask about. Not used.

        Returns:
            What YouTube allows.
        """
        return Limits(
            # The description, which is what `Post.text` becomes. YouTube
            # counts it in bytes, so an emoji costs four of the 5,000 and an
            # accented letter two.
            max_text_length=MAX_DESCRIPTION_BYTES,
            text_counted_in=TextCount.UTF8_BYTES,
            # The title is its own setting in `Post.options`, so `check_post`
            # cannot see it. Saying the number here is what lets an app show
            # somebody the cap before they type past it.
            max_title_length=MAX_TITLE_CHARACTERS,
            # No pictures at all, so there is no number to give. `None`
            # means "we do not know"; the refusal comes from the missing
            # `Feature.POST_IMAGE` instead, which says the true thing.
            max_images=None,
            max_videos=MAX_VIDEOS_PER_POST,
            max_video_bytes=MAX_VIDEO_BYTES,
        )

    async def start_login(self, request: LoginRequest) -> SendToNetwork:
        """Build the address to send somebody to so they can approve your app.

        Nothing is sent to Google here. The address carries the hash of a
        secret; the secret itself comes back to you in `remember`, and is
        sent later, in `finish_login`.

        Args:
            request: Where to send them back to, what to ask for, and your
                app's credentials.

        Returns:
            The address to redirect their browser to, the state value that
            will come back with them, and the secret to hand back.

        Raises:
            ConfigError: If there are no app credentials anywhere.
        """
        app = _app_or_refuse(request.app, "sign somebody in")

        state = request.state or secrets.token_urlsafe(_STATE_BYTES)
        verifier = secrets.token_urlsafe(_VERIFIER_BYTES)

        query = httpx.QueryParams(
            {
                "response_type": "code",
                "client_id": app.client_id,
                "redirect_uri": request.redirect_uri,
                "scope": " ".join(request.scopes or DEFAULT_SCOPES),
                "state": state,
                "code_challenge": _challenge_for(verifier),
                "code_challenge_method": "S256",
                # These two are the difference between an account that keeps
                # working and one that stops in an hour. Without
                # access_type=offline Google issues no refresh token; without
                # prompt=consent it silently skips the refresh token for
                # anyone who has approved this app before, which is worse,
                # because it works the first time you test it.
                "access_type": "offline",
                "prompt": "consent",
            }
        )
        return SendToNetwork(
            url=f"{SIGN_IN_URL}?{query}",
            state=state,
            remember={"code_verifier": verifier},
        )

    async def finish_login(
        self,
        request: LoginRequest,
        callback: Mapping[str, str],
        remember: RawData | None = None,
    ) -> ChooseAccount:
        """Swap the code Google sent back for a token, then ask which channel.

        One Google account can own several channels, so this never finishes
        the job on its own. Show the channels it hands back and call
        `resume_login` with the one the person picked.

        Args:
            request: The same request used to start the login.
            callback: The query values Google sent back. It must have
                `code`; `state` is checked when it is there.
            remember: What `start_login` put in `SendToNetwork.remember`.

        Returns:
            The channels to choose from, and a `resume_token` to hand back.
            That token holds the tokens Google just issued, because the code
            can only be swapped once - keep it with the person's session and
            treat it as a secret.

        Raises:
            AuthError: If the person said no, if there is no code, if the
                state does not match, if the secret from `start_login` did
                not come back, if Google issued no refresh token, or if this
                Google account has no YouTube channel.
            ConfigError: If there are no app credentials anywhere.
            PlatformError: If Google answered without an access token.
        """
        app = _app_or_refuse(request.app, "sign somebody in")
        _check_state(request, callback)
        code = _code_from(callback)
        verifier = _verifier_from(remember)

        asked_for = request.scopes or DEFAULT_SCOPES
        form: dict[str, Any] = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": app.client_id,
            "client_secret": app.client_secret,
            "redirect_uri": request.redirect_uri,
            # The other half of the pair from `start_login`. Google hashes it
            # and checks the result against what it was sent earlier.
            "code_verifier": verifier,
        }

        async with self._client() as http:
            reply = await http.json("POST", TOKEN_URL, data=form)

        access_token = _text(reply, "access_token", "sign someone in")
        refresh_token = reply.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            message = (
                "Google signed this person in but sent no refresh token, so "
                "the connection would stop working in about an hour. That "
                "happens when the sign-in address is missing "
                "access_type=offline, or when it is missing prompt=consent "
                "and the person has approved this app before. socialchimp "
                "sends both, so a login started somewhere else - or an "
                "address built by hand - is the usual cause. Nothing has "
                "been saved; start the login again through start_login."
            )
            raise AuthError(message, platform=PLATFORM_NAME, raw=reply)

        async with self._client(API_URL, access_token) as http:
            mine = await http.json(
                "GET", "/channels", params={"mine": "true", "part": "snippet"}
            )

        channels = {
            _text(item, "id", "list this person's channels"): _channel_name(item)
            for item in _items_in(mine)
        }
        if not channels:
            message = (
                "This Google account has no YouTube channel, so there is "
                "nothing to connect. A Google account and a YouTube channel "
                "are different things - the person can make one at "
                "https://www.youtube.com/create_channel and then sign in "
                "again."
            )
            raise AuthError(message, platform=PLATFORM_NAME, raw=mine)

        granted = reply.get("scope")
        given = granted.split() if isinstance(granted, str) and granted else []

        return ChooseAccount(
            options=tuple(
                AccountChoice(id=channel_id, name=name, kind="channel")
                for channel_id, name in channels.items()
            ),
            resume_token=_pack(
                {
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "expires_at": _expiry_from(reply).isoformat(),
                    "scopes": given or list(asked_for),
                    "channels": channels,
                }
            ),
        )

    async def resume_login(
        self,
        request: LoginRequest,
        *,
        resume_token: str,
        account_id: str,
        remember: RawData | None = None,
    ) -> Finished:
        """Finish the login now that a channel has been picked.

        Nothing is sent to Google here. The tokens were issued during
        `finish_login` and travelled back in the `resume_token`, because
        Google's code can only be swapped once.

        Args:
            request: The same request the login was started with. Not used.
            resume_token: The value from `ChooseAccount`, handed straight
                back.
            account_id: Which channel the person picked.
            remember: What `start_login` put in `SendToNetwork.remember`.
                Not needed again.

        Returns:
            The finished connection. Save it.

        Raises:
            AuthError: If the resume token cannot be read, or if the channel
                is not one this person was actually offered.
        """
        kept = _unpack(resume_token)
        channels = kept.get("channels")
        offered = channels if isinstance(channels, dict) else {}

        if account_id not in offered:
            message = (
                f"{account_id!r} was not one of the channels this person "
                f"approved. They were offered: "
                f"{', '.join(sorted(offered)) or 'none'}. Pass one of those "
                f"as account_id, or start the login again."
            )
            raise AuthError(message, platform=PLATFORM_NAME)

        scopes = kept.get("scopes")
        expires_at = _moment(str(kept.get("expires_at", "")))

        return Finished(
            connection=Connection(
                id=f"{PLATFORM_NAME}:{account_id}",
                platform=PLATFORM_NAME,
                host=None,
                account_id=account_id,
                account_name=str(offered[account_id]),
                token=Token(
                    access_token=str(kept.get("access_token", "")),
                    refresh_token=str(kept.get("refresh_token", "")),
                    expires_at=expires_at,
                ),
                scopes=tuple(str(one) for one in scopes)
                if isinstance(scopes, list)
                else (),
                extra={
                    "channel_id": account_id,
                    "channel_url": f"https://www.youtube.com/channel/{account_id}",
                },
            )
        )

    async def refresh(
        self,
        connection: Connection,
        app: AppCredentials | None = None,
    ) -> Token:
        """Get a fresh access token for a channel.

        Google does **not** rotate refresh tokens. A renewal answers with a
        new access token and, almost always, nothing else - so the refresh
        token already on the connection is carried across. That is the
        opposite of Bluesky, where using a refresh token destroys it, and
        socialchimp's token code is written for that case. Reading the
        refresh token straight out of this reply would set it to nothing and
        lock the person out at the next renewal.

        Args:
            connection: The account whose token is running out.
            app: Your app's client id and secret. Google signs a renewal
                with both, so this is not optional here - `SocialChimp`
                reads them out of your storage and passes them in.

        Returns:
            The new access token, with the refresh token we already had.

        Raises:
            ConfigError: If no credentials arrived. Google will not renew
                without them, so save them with `Storage.save_app`.
            TokenExpiredError: If there is no refresh token, or Google will
                not take the one we have.
            PlatformError: If Google answered without a token.
        """
        signing = _app_or_refuse(app, "renew a token")

        renewal = connection.token.refresh_token
        if renewal is None:
            message = (
                f"The token for {connection.id!r} has run out and there is "
                f"no refresh token to replace it with. Check the sign-in "
                f"asked for access_type=offline and prompt=consent - without "
                f"both, Google never sends one. The person has to connect "
                f"their channel again."
            )
            raise TokenExpiredError(message, platform=PLATFORM_NAME)

        form: dict[str, Any] = {
            "grant_type": "refresh_token",
            "refresh_token": renewal,
            "client_id": signing.client_id,
            "client_secret": signing.client_secret,
        }

        async with self._client() as http:
            try:
                reply = await http.json("POST", TOKEN_URL, data=form)
            except (AuthError, PlatformError) as refused:
                # Google having trouble of its own is not the same as a dead
                # refresh token, and treating it as one would have apps
                # throwing away connections that were fine.
                if _is_googles_own_fault(refused):
                    raise
                message = (
                    f"Google will not renew the token for {connection.id!r}. "
                    f"Its refresh token has been revoked, or the person "
                    f"removed your app from their Google account, or the app "
                    f"is still unreviewed and its test tokens expire after a "
                    f"week. The person has to connect their channel again."
                )
                raise TokenExpiredError(
                    message, platform=PLATFORM_NAME, raw=refused.raw
                ) from refused

        # Google usually sends no refresh token back at all. On the rare
        # occasion it does, that one wins.
        replacement = reply.get("refresh_token")
        return Token(
            access_token=_text(reply, "access_token", "renew a token"),
            refresh_token=replacement
            if isinstance(replacement, str) and replacement
            else renewal,
            expires_at=_expiry_from(reply),
        )

    async def publish(self, connection: Connection, post: Post) -> PostResult:
        """Upload a video.

        The video is sent to YouTube a piece at a time. YouTube says after
        every piece how much of it arrived, and the next piece starts from
        there rather than from where we assumed - which is the whole point
        of uploading this way, and the difference between an upload that
        survives a dropped connection and one that quietly leaves a hole in
        the middle of the video.

        Args:
            connection: The channel to publish as.
            post: What to publish. `Post.text` becomes the description, and
                `Post.options` must carry `title` and `made_for_kids`.

        Returns:
            What YouTube said. The state is `PostState.PROCESSING`, not
            `DONE`: taking the bytes is not publishing, and encoding carries
            on for minutes or hours afterwards. Ask `check_state` later. A
            video with a publishing time comes back as
            `PostState.SCHEDULED`.

        Raises:
            NotSupportedError: If the post has no video. YouTube has no
                text-only post to fall back to.
            InvalidPostError: If the post breaks one of YouTube's rules, or
                if a setting is missing or wrong.
            PlatformError: If YouTube never hands back a session address, if
                it keeps saying nothing arrived, or if it takes every byte
                and never says what the video is.
        """
        # The order here matters. The shared checks go first so that a post
        # over a limit is refused as an InvalidPostError, whatever else is
        # wrong with it; the missing video is part of those now, and comes
        # before the missing title, because it is the more fundamental
        # problem of the two.
        limits = await self.limits(connection)
        check_post(
            post,
            platform=PLATFORM_NAME,
            features=self.features,
            limits=limits,
            words_alone_advice=WORDS_ALONE_ADVICE,
        )
        video = _the_video(post)
        metadata, notify = _metadata_for(post, limits)
        total = _how_big(video)

        async with self._client(token=connection.token.access_token) as http:
            where = await self._start_upload(http, metadata, notify, video, total)
            reply = await self._send_video(http, where, video, total)

        video_id = _text(reply, "id", "publish a video")
        return PostResult(
            id=video_id,
            url=_watch_url(video_id),
            state=(
                PostState.SCHEDULED
                if post.publish_at is not None
                else PostState.PROCESSING
            ),
            raw=reply,
        )

    async def _start_upload(
        self,
        http: HttpClient,
        metadata: RawData,
        notify: bool | None,
        video: Media,
        total: int,
    ) -> str:
        """Tell YouTube a video is coming, and get somewhere to send it.

        Args:
            http: A client that carries whole addresses.
            metadata: The title, description and settings.
            notify: Whether to tell subscribers, or `None` to leave it to
                YouTube's own default.
            video: The video about to be sent.
            total: How many bytes of it there are.

        Returns:
            The address to send the bytes to.

        Raises:
            PlatformError: If YouTube answers without one.
        """
        params: dict[str, str] = {"uploadType": "resumable", "part": "snippet,status"}
        if notify is not None:
            params["notifySubscribers"] = "true" if notify else "false"

        response = await http.post(
            UPLOAD_URL,
            params=params,
            json=metadata,
            # YouTube uses these to decide whether it will take the file at
            # all, before a single byte of it is sent.
            headers={
                "X-Upload-Content-Length": str(total),
                "X-Upload-Content-Type": video.content_type,
            },
        )

        where: str | None = response.headers.get("location")
        if not where:
            message = (
                "YouTube agreed to take a video but sent no Location header, "
                "so there is nowhere to send it. Nothing has been uploaded. "
                "Trying again is safe."
            )
            raise PlatformError(
                message,
                platform=PLATFORM_NAME,
                status_code=response.status_code,
                raw=read_body(response),
            )
        return where

    async def _send_video(
        self,
        http: HttpClient,
        where: str,
        video: Media,
        total: int,
    ) -> RawData:
        """Send the video, a piece at a time, and hand back what YouTube said.

        Args:
            http: A client that carries whole addresses, so the session
                address is used exactly as YouTube gave it.
            where: The session address from `_start_upload`.
            video: The video to send. Only the piece on its way to YouTube
                is ever in memory, because `Media.piece` reads it off disk
                as it goes.
            total: How many bytes of it there are.

        Returns:
            The video as YouTube now holds it.

        Raises:
            PlatformError: If YouTube keeps saying none of a piece arrived,
                or if it takes every byte and never says what the video is.
        """
        sent = 0
        resends = 0

        while sent < total:
            last = min(sent + self._chunk_bytes, total) - 1
            response = await http.put(
                where,
                content=video.piece(sent, last - sent + 1),
                headers={
                    "Content-Range": f"bytes {sent}-{last}/{total}",
                    "Content-Type": video.content_type,
                },
            )

            # 308 is a redirect everywhere else on the web. Here it means
            # "keep going", and the Range header says how much really landed
            # - which can be less than we sent.
            if response.status_code != httpx.codes.PERMANENT_REDIRECT:
                return read_body(response)

            arrived = _arrived_so_far(response)
            if arrived <= sent:
                resends += 1
                if resends > self._resends_allowed:
                    message = (
                        f"YouTube says none of it arrived after "
                        f"{resends} tries at the piece starting at byte "
                        f"{sent}. Something between here and YouTube is "
                        f"dropping the upload. The session is still open, so "
                        f"trying the whole post again is safe."
                    )
                    raise PlatformError(message, platform=PLATFORM_NAME)
            else:
                resends = 0
            sent = arrived

        message = (
            f"YouTube took all {total} bytes of the video but never "
            f"answered with the video itself, so there is no id to hand "
            f"back. The upload may well have worked - look at the channel "
            f"before sending it again."
        )
        raise PlatformError(message, platform=PLATFORM_NAME)

    async def check_state(self, connection: Connection, post_id: str) -> PostResult:
        """Ask YouTube how far it has got with a video.

        `publish` comes back while YouTube is still encoding, so this is how
        you find out what happened next. It costs one unit of the daily
        quota, so it is cheap to call on a timer - unlike an upload, which
        costs about 1,600.

        Args:
            connection: The channel the video is on.
            post_id: YouTube's id for the video.

        Returns:
            Where the video has got to. `PostState.PROCESSING` while YouTube
            is still working, `DONE` once it is live, `SCHEDULED` while it
            waits for its moment, and `FAILED` if YouTube gave up on it or
            turned it down.

        Raises:
            NotFoundError: If there is no such video on this channel.
        """
        async with self._client(API_URL, connection.token.access_token) as http:
            reply = await http.json(
                "GET",
                "/videos",
                params={"part": "status,processingDetails", "id": post_id},
            )

        found = _items_in(reply)
        if not found:
            message = (
                f"YouTube has no video {post_id!r} on this channel. It may "
                f"have been deleted, or it may belong to a different "
                f"channel than the one this connection is for."
            )
            raise NotFoundError(message, platform=PLATFORM_NAME, raw=reply)

        video = found[0]
        status = video.get("status")
        status = status if isinstance(status, dict) else {}
        reported = str(status.get("uploadStatus"))
        state = _OUR_STATE_FOR.get(reported, PostState.PROCESSING)

        # A video YouTube has finished with, that is still private and has a
        # moment to go public, is waiting rather than done.
        if state is PostState.DONE and status.get("publishAt"):
            state = PostState.SCHEDULED

        return PostResult(
            id=post_id,
            url=_watch_url(post_id),
            state=state,
            raw=video,
        )

    # YouTube can push as well, through WebSub, but only to say that a
    # channel has a new video. There is no push for comments, likes or
    # anything else, and comments are what apps ask for - so this is what
    # socialchimp uses, and `Feature.PUSH_UPDATES` stays off rather than
    # promising something that only covers uploads we made ourselves.
    async def fetch_updates(
        self,
        connection: Connection,
        since: datetime | None,
    ) -> Sequence[Update]:
        """Return the comments left on this channel since a moment in time.

        YouTube pages comments rather than filtering them by time, so we
        read a recent page and drop anything older than the marker. Check
        often enough that a page covers the gap.

        Args:
            connection: The channel to ask about.
            since: Only return comments newer than this. `None` on the first
                call.

        Returns:
            The comments, oldest first.
        """
        params = {
            "part": "snippet",
            "allThreadsRelatedToChannelId": _channel_of(connection),
            "order": "time",
            "maxResults": str(self._updates_per_check),
        }

        async with self._client(API_URL, connection.token.access_token) as http:
            reply = await http.json("GET", "/commentThreads", params=params)

        updates: list[Update] = []
        for thread in _items_in(reply):
            when = _moment(_written_at(thread))
            if when is None or (since is not None and when <= since):
                continue
            updates.append(
                Update.from_network(
                    update_id=str(thread.get("id", "")),
                    kind_name="comment_created",
                    platform=PLATFORM_NAME,
                    connection_id=connection.id,
                    created_at=when,
                    raw=thread,
                )
            )

        # YouTube hands back the newest first; socialchimp wants the oldest.
        updates.reverse()
        return updates


def _watch_url(video_id: str) -> str:
    """Return the address a person would use to watch a video.

    Args:
        video_id: YouTube's id for the video.

    Returns:
        The address. A Short works at this address too - YouTube sends
        people on to its own player.
    """
    return f"https://www.youtube.com/watch?v={video_id}"


def _channel_name(item: RawData) -> str:
    """Read a channel's name out of what YouTube said about it.

    Args:
        item: One channel from `channels.list`.

    Returns:
        Its name, or its id when YouTube left the name out.
    """
    snippet = item.get("snippet")
    if isinstance(snippet, dict):
        title = snippet.get("title")
        if isinstance(title, str) and title:
            return title
    return str(item.get("id", ""))


def _channel_of(connection: Connection) -> str:
    """Work out which channel a connection is for.

    Args:
        connection: The account to look at.

    Returns:
        The channel id, from `extra` where a login put it, and from
        `account_id` for a connection saved before that.
    """
    saved = connection.extra.get("channel_id")
    return saved if isinstance(saved, str) and saved else connection.account_id


def _written_at(thread: RawData) -> str:
    """Dig the time out of a comment thread.

    YouTube buries it two objects down, under the top comment rather than
    on the thread itself.

    Args:
        thread: One thread from `commentThreads.list`.

    Returns:
        The time as YouTube wrote it, or an empty string.
    """
    snippet = thread.get("snippet")
    top = snippet.get("topLevelComment") if isinstance(snippet, dict) else None
    inner = top.get("snippet") if isinstance(top, dict) else None
    if isinstance(inner, dict):
        return str(inner.get("publishedAt", ""))
    return ""


def _expiry_from(reply: RawData) -> datetime:
    """Work out when an access token stops working.

    Args:
        reply: What Google's token endpoint answered.

    Returns:
        The moment it runs out. Google's tokens last about an hour, and that
        is what is assumed when it does not say.
    """
    seconds = reply.get("expires_in")
    lasts = seconds if isinstance(seconds, int) else 3600
    return datetime.now(UTC) + timedelta(seconds=lasts)
