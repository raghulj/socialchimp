# Changelog

Notable changes, newest first. Versions follow
[semantic versioning](https://semver.org): while this is 0.x, a change to the
middle number may break something.

## 0.3.1 - 2026-08-31

### Fixed

- **`socialchimp.testing` no longer imports pytest.** `from
  socialchimp.testing import FakePlatform` raised `ModuleNotFoundError: No
  module named 'pytest'` on an install without the `testing` extra, because
  the import sat at the top of the module. Only `PlatformChecks` ever wanted
  it. `FakePlatform`, `RecordingStorage`, `RecordingTransport` and
  `StorageCall` are for building an app as much as for testing one - the
  sample projects in `examples/` build a whole app against `FakePlatform`
  and nothing else - so a fake social network no longer asks for a test
  framework.

  pytest is now imported the first time a check fails or skips, and
  subclassing `PlatformChecks` without it raises a `ConfigError` naming
  `pip install "socialchimp[testing]"` rather than a bare
  `ModuleNotFoundError`. Nothing changes for anyone who has pytest, and the
  extra still installs it.

## 0.3.0 - 2026-08-31

### Read this first: `Dispatcher.deliver` raises now, and stops losing updates

`Dispatcher.deliver` used to log a handler that raised and then carry on as
though nothing had happened - including writing the update down as handled.
So when every handler failed, the update was still recorded as done, the
network's retry was skipped by the `seen` check, and the update was gone. A
log line was all that was left of it.

It still runs every handler, and one that raises still does not stop the
rest. What changed is what happens afterwards:

- **The update is remembered as handled only if every handler succeeded.** If
  any raised, nothing is written down, so the network's retry is a real
  second chance instead of something the `seen` check throws away.
- **The failures come back to you**, as an `ExceptionGroup` holding what each
  failed handler raised.

It is always a group, even when only one handler failed. That way there is
one shape to catch, and registering a second handler tomorrow does not change
what your code has to catch today.

```python
# Before - this could not raise, so there was nothing to write.
await dispatcher.deliver(update)

# Now, if you want to carry on regardless.
try:
    await dispatcher.deliver(update)
except* Exception:
    logger.exception("a handler for %s failed", update.id)
```

**What to change.** If you call `deliver` yourself and relied on it never
raising, decide what should happen and write it, as above.

If you use the Django, FastAPI or Flask routes there is nothing to change,
but the behaviour is different on purpose: a webhook whose handlers all
failed no longer answers `200 {"ok": true}`. The group goes up, your
framework answers 500, and the network sends the update again - to a
dispatcher that did not write it down as handled either. See
[handlers that fail](docs/frameworks.md#handlers-that-fail).

**Why.** socialchimp raises, your app handles. A handler failing is your code
failing, and only your app knows whether that deserves a log line, an alert,
or a row in a table for a worker to retry tonight. The old docstring even
defended the ordering by saying that a crash gives the network a second
chance - while the code underneath made sure that crash never happened.

### Webhook routes: no silent drops, and set-up mistakes are not 500s

Two changes to `socialchimp.contrib.shared.Routes`, and so to the Django,
FastAPI and Flask helpers built on it.

**`Routes` with webhook secrets and no `deliver` is refused when it is
built.** It used to be accepted, and then every properly signed update was
logged, thrown away, and answered `200 {"ok": true}` - so the network
believed it had been handled and never sent it again. That is knowable at
set-up time, so it is now a `ConfigError` from `Routes(...)`, next to the one
for a missing webhook secret. Routes that only sign people in are unaffected:
no secrets means no webhooks, so there is nothing to hand on.

```python
# Refused now.
Routes(sc, redirect_uri=..., secrets={"facebook": SECRET})

# Hand updates somewhere,
Routes(
    sc,
    redirect_uri=...,
    secrets={"facebook": SECRET},
    deliver=dispatcher.deliver,
)


# or, while you are only getting the URL verified, say so out loud.
async def note_it(update: Update) -> None:
    logger.info("dropping %s for now", update.id)
```

**`ConfigError` is raised rather than answered.** Every `Routes` method used
to catch it two lines from where it was raised and turn it into a 500 with a
JSON body, on every request, for ever. A missing webhook secret, a missing
setup token, an app that was never registered with the network - these are
mistakes to fix, not conditions to retry, and they now reach your own error
handling instead of being reported like a network problem.

Everything else is unchanged: whatever a network said no to, and whatever was
wrong with the request itself, still becomes a `Reply` with the status it
deserves.

**What to change.** If you have a test asserting a 500 and a JSON body for
one of these, it should expect a `ConfigError` now. If you wrap these routes
in something of your own and would rather answer than raise, `status_for` and
`Reply.for_error` still map `ConfigError` to 500 - it is one
`except ConfigError` away.

### Fixed: Facebook said `PROCESSING` and gave you no way to ask

`FacebookPlatform.publish` has always answered a video with
`PostState.PROCESSING`, because Facebook takes the bytes and carries on
encoding after it replies. But `FacebookPlatform` had no `check_state`, so
`account.check_state(post_id)` raised `NotSupportedError` — the state said
"ask again later" and there was nothing to ask.

There is now a `check_state` here, the same shape as YouTube's and TikTok's:

```python
result = await account.post(Post(text="Watch this", media=(clip,)))
if result.state is PostState.PROCESSING:
    later = await account.check_state(result.id)
```

It reads the video's `status` — one field, one cheap request — and maps
Facebook's three words onto ours: `ready` is `DONE`, `processing` is
`PROCESSING`, `error` is `FAILED`. Anything else, including a word Meta adds
next year, comes back `PROCESSING`, so an unfamiliar answer means "ask again"
rather than "it is live". The address on the result is the one `publish` gave,
so results from either can be treated the same way.

Only a video needs it. Words and pictures are on the page the moment `publish`
returns, and asking about one of those raises a `PlatformError` that says so.

**Instagram and Threads do not need the same thing and have not gained it.**
Both publish in two steps, and both do their waiting inside `publish` — they
poll the container until Meta says `FINISHED` and only then publish it. So
neither ever hands back `PostState.PROCESSING`, and a `check_state` there
would have nothing to answer.

### Fixed: `Media.alt_text` was thrown away on X

`Media.alt_text` reached Bluesky, Facebook, Instagram, Mastodon and Threads,
and X dropped it on the floor — silently, with no error and nothing in the
result to notice it by. An app that sets alt text everywhere was publishing
pictures on X that a screen reader could not describe.

X does support it; it is just a request of its own, because the upload has
nowhere to carry a description. `XPlatform` now sends one:

```python
Post(media=(Media.from_file("shop.jpg", alt_text="A shop front"),))
# ... POST /2/media/metadata  {"id": ..., "metadata": {"alt_text": {"text": ...}}}
```

It goes out after the file is finalised — and, for video, after X has finished
encoding it — and always before the file is named on a post, because X will
not take a description for one that is already published. A file with no
`alt_text` sends nothing extra, so nothing costs a request that did not
before.

**Nothing to change in your code.** If you were already setting `alt_text`, it
now arrives.

The two networks that still do not take it are honest about why, and
[docs/platforms.md](docs/platforms.md#alt-text) now says so in one place:
Pinterest hangs alt text off the whole pin rather than off one picture, so it
stays `options={"alt_text": ...}` there; YouTube and TikTok take video only,
and neither has alt text for a video.

### Documented: which networks stream a video, and which read it whole

[docs/platforms.md](docs/platforms.md#how-much-of-a-video-has-to-fit-in-memory)
said that YouTube, TikTok and X send a video in pieces, and said nothing about
the two that do not. **Facebook and Pinterest read the whole file into memory
first** — Facebook's chunked upload is not written yet, and Pinterest hands
out one upload form for one request — so a video there really does cost its
own size in memory on your own server. That is now written down next to the
claims about the ones that stream. No behaviour changed; Facebook's
`biggest_video_bytes` was always the lever, and now the page says what it is
for.

### Read this first: `post_to_many` is gone. Write the loop yourself.

`SocialChimp.post_to_many`, `PostJob` and `PostError` have been removed. There
is no replacement call, because posting to several accounts is now your loop.

```python
# Before.
job = await sc.post_to_many([mastodon_id, bluesky_id], Post(text="Hi"))

for result in job.succeeded:
    print("posted:", result.url)
for failure in job.failed:
    print("failed:", failure.connection_id, failure.error)

# Now.
from socialchimp import SocialChimpError

for connection_id in (mastodon_id, bluesky_id):
    try:
        result = await sc.account(connection_id).post(Post(text="Hi"))
        print("posted:", result.url)
    except SocialChimpError as refused:
        print("failed:", connection_id, refused)
```

**What to change.**

- `await sc.post_to_many(ids, post)` becomes a `for` loop over `ids` calling
  `await sc.account(connection_id).post(post)`.
- `job.succeeded` becomes whatever you append a `PostResult` to in the loop.
- `job.failed` becomes your `except` block. Catch `SocialChimpError` for
  everything socialchimp raises, or something narrower - `RateLimitError`,
  `AuthError`, `NotSupportedError` - to treat one kind differently.
- `post_to_many(..., options_per_platform={"youtube": {...}})` has no
  replacement argument. Build the post each network needs inside your loop
  with `dataclasses.replace(post, options={**post.options, **extra})`.
- Anywhere you imported `PostJob` or `PostError`, delete the import. The
  error types you catch - `SocialChimpError` and everything under it - are
  unchanged, and they are the part that was doing the work.

**Why.** socialchimp raises and stops; your app catches and decides. Whether
one network refusing should stop the others, whether the failure belongs in a
row for a worker to retry tonight, whether somebody needs telling - those are
answers only your app has. `post_to_many` had to pick one of them for you, and
"write it down and carry on" is the wrong answer often enough to matter. The
loop is four lines and it is honest about who is deciding.

Nothing else changed. Retries, rate-limit handling and token renewal are
exactly as they were, and every error type is where it was.

There is a runnable version, with one network refusing and the app carrying
on, in `examples/post_to_each.py`.

### Fixed: Bluesky would not start a login without credentials it cannot have

`sc.start_login("bluesky", redirect_uri="unused")` raised `ConfigError` saying
no app credentials were stored. Bluesky has no developer portal and no app —
a person signs in with their handle and an app password they made themselves
— so there was nothing to store, and no way to make the message come true.
The example in [getting started](docs/getting-started.md#networks-with-no-sign-in-page)
raised on the line it was printed on, and apps worked around it by saving a
placeholder id and secret that nothing ever read.

There is a new feature flag, and Bluesky lists it:

```python
Feature.NEEDS_NO_APP in BlueskyPlatform.features  # True
```

Where it is on, `start_login`, `finish_login` and `choose` ask your storage
for nothing and hand the platform a `LoginRequest` with `app=None`. Where it
is off — every other network, Mastodon included, because Mastodon registers a
real app for you and the sign-in needs it — a sign-in with none saved is
refused exactly as before, naming `Storage.save_app`.

`sc.create_app("bluesky", ...)` was telling the same untruth from the other
side, sending people to a developer portal that does not exist. It now says
there is no app to register and that `start_login` works with nothing saved.

**What to change.** Delete any placeholder credentials you saved for Bluesky;
nothing reads them. If you wrote your own platform for a network with no app
of its own, add `Feature.NEEDS_NO_APP` to its `features` — see
[adding a platform](docs/adding-a-platform.md#rules-that-matter-more-than-they-look).
A platform that does not list it behaves exactly as it did.

### Fixed: every `FakePlatform` connection had the same id

`FakePlatform.connection()` handed back `"fake-connection"` whatever the fake
was called, so an app testing across nine fake networks got nine connections
sharing one primary key — and the docs tell people to match pushed updates on
that id. The default is now the network's name and the account's id joined by
a colon, which is what every real platform does:

```python
FakePlatform(name="bluesky").connection().id  # "bluesky:42"
FakePlatform().connection(account_id="7").id  # "fake:7"
FakePlatform().connection(connection_id="mine").id  # "mine", as before
```

**What to change.** A test that expects the literal `"fake-connection"`
should expect `"fake:42"`, or pass `connection_id="fake-connection"` to keep
the old one.

### Added: `FakePlatform` answers Meta's setup check

Testing your `hub.challenge` route against a fake meant subclassing
`FakePlatform` and calling `socialchimp.events.answer_setup_check` yourself.
The fake now has an `answer_setup_check` of its own, the same function
underneath that Facebook, Instagram and Threads use:

```python
sc = SocialChimp(storage=storage, platforms={"fake": FakePlatform()})
challenge = sc.answer_setup_check(
    "fake",
    {"hub.mode": "subscribe", "hub.verify_token": TOKEN, "hub.challenge": "1158201444"},
    verify_token=TOKEN,
)
```

`FakePlatform(answers_setup_checks=False)` leaves it off entirely, so the fake
is not a `CanAnswerSetupCheck` and `sc.answer_setup_check` refuses against it
— the way it refuses against TikTok, which pushes without asking anything
first. That is the same knob-decides-the-ability pattern `accounts` uses for
`resume_login` and `states` uses for `check_state`.

### Fixed: the refusal for a network that never pushes said something untrue

`SocialChimp.answer_setup_check` refused with one fixed sentence — "It starts
sending as soon as you point it at a URL" — whoever asked. That is right for
TikTok. For Pinterest, which never pushes anything to a URL of yours, it
describes something that will not happen and leaves somebody waiting for it.

The message now depends on what the network can actually do. A network that
pushes but asks nothing first is told what it was told before. A network with
no `Feature.PUSH_UPDATES` is told that it never pushes and pointed at
`Account.fetch_updates` and `socialchimp.events.Poller`, which is the same
answer `check_signature` already gave for the same networks.

### Read this first: `SocialChimpError` really does catch everything now

The rule is that socialchimp raises and your app handles, and the whole of
that rests on one line in the docs: catch `SocialChimpError` and you have
caught everything socialchimp reports. It was not true. Every refusal in
`socialchimp.models` came out as a bare `ValueError` and walked straight past
`except SocialChimpError`:

```python
friday = datetime(2026, 9, 4, 9, 0)  # no timezone on it

Post()  # neither text nor media
Post(text="hi", publish_at=friday)  # a time with no timezone
Token(access_token="abc", expires_at=friday)  # and again
Media.from_bytes(b"...", filename="cat.xyz")  # an ending we do not know
Media.from_url("https://...").read()  # no bytes to read yet
Media.from_url("https://...").piece(0, 1)  # nor here
```

The first four are the everyday ones. The last two are the ones that bit: a
`Media.from_url` handed to a network that will not fetch it crashed the app
rather than being reported like every other bad post.

**What they raise now.** A bad post or a picture we cannot read is an
`InvalidPostError`. A datetime with no timezone is a `ConfigError` - it is a
mistake in your code rather than a post any network would refuse, and the
same check guards a token's expiry and an update's timestamp, where "post"
means nothing.

**Nothing you have written stops working.** `ConfigError` and
`InvalidPostError` are now a `ValueError` as well as a `SocialChimpError`, so
an app that noticed the old behaviour and caught `ValueError` catches these
exactly as before. Being both is unusual, and there is a comment in
`socialchimp/errors.py` saying why: these are the two raised for a value your
code handed us, and they are the two that used to be a plain `ValueError`.

**What to change.** Nothing, unless you would rather catch one thing than
two - in which case delete the `except ValueError` you wrote to work around
this and let `except SocialChimpError` do it.

No built-in network was relying on the old behaviour, and none of their
messages has changed. Bluesky, Mastodon, TikTok, X and YouTube each refuse a
`Media.from_url` before reading it, naming themselves and saying to download
the file first; Facebook and Instagram fetch the address themselves; and
Pinterest fetches a picture and refuses a video the same way. Those are still
the messages you will see. What changed is the answer underneath them - which
is what a platform written by somebody else, or a `Media.read()` of your own,
runs into.

### Added: `PlatformChecks` holds you to `Feature.NEEDS_NO_APP`

`Feature.NEEDS_NO_APP` arrived earlier in this release for Bluesky, which has
no developer portal and no app. A platform claiming the flag and then
refusing a sign-in without credentials would be the worst of both: nothing to
save, and a login that will not start, with a message telling somebody to
save credentials that do not exist.

There is a check for it now, so a platform published by anyone inherits it:

```python
class TestMyPlatform(PlatformChecks):
    def make_platform(self) -> Platform:
        return MyPlatform()
```

It starts a login with `LoginRequest.app` as `None` and fails if the platform
refuses. A platform that does not list the flag skips it, and a network that
really does need credentials is refused without them exactly as before.

## 0.2.0 - 2026-08-31

### Read this first: `Update.raw` from Facebook, Instagram and Threads changed

`Update.raw` is now **the one thing that happened**, not the message it
arrived in.

One message from Meta holds a list of pages, and under each page a list of
changes. `read_updates` has always given you one `Update` per change - but it
put the whole page entry on every one of them, so a handler could not tell
which change its update was about and had to go looking for it again:

```python
# Before - and it had to be written this way, because raw was the entry.
for change in update.raw.get("changes", []):
    value = change.get("value", {})
    if value.get("item") != "comment":
        continue
    print(value.get("message"))

# Now.
print(update.raw.get("message"))
```

**What to change.** Anywhere you read `update.raw` on a Facebook, Instagram
or Threads update:

- `update.raw["changes"][n]["value"][k]` becomes `update.raw[k]`.
- Threads' `update.raw["values"]["value"][k]` becomes `update.raw[k]`.
- The page id and the time that used to be on `raw` are on the new
  `update.envelope`, which holds the entry the change arrived in. So
  `update.raw["id"]` becomes `update.envelope["id"]`, and
  `update.raw["time"]` becomes `update.envelope["time"]`.

Nothing is lost - `envelope` keeps everything `raw` used to hold - and
nothing else changed. `Update.raw` on TikTok, X, Mastodon, Bluesky and
YouTube was already the thing that happened and is untouched.

This is a break in a minor release, and it is deliberate. The old shape made
every Meta handler wrong in the same way: it looped, it re-filtered, and if a
busy moment put two comments in one message it printed both of them twice.

### Added

- **`Account.check_state(post_id)`.** YouTube and TikTok keep working after
  they accept a post, so a `PostResult` that came back `PROCESSING` is not
  the end of it. This asks how far they have got, renewing the token first
  the way every other call on an `Account` does. Networks that finish before
  they answer raise `NotSupportedError` naming themselves.
- **`Account.fetch_updates(since=None)`.** The same for networks that have to
  be asked what has happened. Hand it to `Poller` and it runs on a timer.

  Both of these were reachable before only by building the platform yourself
  and passing it to `SocialChimp(platforms=...)`, because `platform_for`
  hands back the `Platform` protocol and neither method is on it. That works
  and still works; it is no longer the only way. Both also close a seam:
  they take a post id and a marker, where the platform methods behind them
  take a `Connection`, which is not a thing an app was holding.
- **`SocialChimp.answer_setup_check(platform, params, verify_token=...)`,
  `SocialChimp.check_signature(platform, body, headers, secret=...)` and
  `SocialChimp.read_updates(platform, body)`.** The same three calls for the
  requests a network pushes to you. They take the network's name rather than
  going through `Account`, the way `start_login` does, because a pushed
  request arrives before you know whose account it concerns - `read_updates`
  is what tells you that. All three are plain functions, so a synchronous
  Django view calls them without a bridge.
- **`CanCheckState`, `CanAnswerSetupCheck` and `CanReadPushedUpdates`** in
  `socialchimp.platform`, beside `CanResumeLogin`. These say the exact shape
  of `check_state`, `answer_setup_check` and `read_updates`, and they are
  what the calls above look for.
- **`Update.envelope`.** The message an update arrived in, where a network
  wraps things up. Empty everywhere else.
- **`TikTokPlatform.read_updates(body)`.** TikTok sends one event per
  message, so it is always a list of one - it is there so
  `SocialChimp.read_updates` reaches every network that pushes.
- **`PlatformChecks` now checks `check_state`.** If your platform has one, it
  must be `async def check_state(self, connection, post_id)`, because that is
  how `Account.check_state` calls it.
- **`testing.FakePlatform` takes `states=`** - what `check_state` says, one
  call after another, with the last repeating. Leave it out and the fake has
  no `check_state` at all, the same as most networks. It also has a
  `read_updates` now, so a fake standing in for a pushing network works with
  `SocialChimp.read_updates`.
- **`testing.FakePlatform` built with `accounts` now satisfies
  `CanResumeLogin`.** Before, a fake with accounts to choose between still
  had no `resume_login`, so calling `sc.choose(...)` against it raised
  `NotSupportedError` even though `finish_login` had just answered
  `ChooseAccount`. It now carries a real `resume_login`, the same as
  Facebook, Instagram and YouTube do, so `choose()` succeeds. If your own
  tests relied on that `NotSupportedError` to prove your app's error
  handling worked, they will now see the login finish instead.

### If you wrote your own platform

Nothing here forces a change. `Platform` is untouched, and so are
`CanCreateApp`, `CanResumeLogin`, `CanDeletePosts`, `CanReadUpdates` and
`CanCheckSignature`.

Two things are worth doing anyway:

- If your network keeps working after it accepts a post, name the method
  `check_state` and give it `(connection, post_id)`. `Account.check_state`
  then finds it, and `PlatformChecks` will tell you if the shape is wrong.
- If your network pushes and you only wrote `read_update`, add a
  `read_updates(body) -> list[Update]`. Without one, `SocialChimp
  .read_updates` refuses with a message saying exactly that, because
  `read_update` hands back the first change and drops the rest.

## 0.1.0 - 2026-08-31

The first release. Nine networks work end to end.

### Networks

**Mastodon**, **Bluesky**, **Facebook Pages**, **Instagram**, **YouTube**,
**TikTok**, **X**, **Pinterest**, **Threads**.

Each one signs people in, keeps their token working, posts, and reports what
happened. What a network cannot do it says so, rather than approximating:
Bluesky has no scheduling, YouTube has no post of words alone, Pinterest has
no comments, Instagram cannot take an upload and needs a web address.

### What is in it

- **One way to work with every network**, and direct access to any of them
  when the shared way is not enough. Direct access still renews your token,
  retries, and respects rate limits - only the request is yours.
- **Your app keeps its own database.** No models, no migrations. Five methods
  in a class you write.
- **Tokens renewed before they run out**, under a lock so two workers cannot
  renew at once. That matters on Bluesky, Pinterest and TikTok, which replace
  the refresh token every time it is used: without the lock, whichever worker
  loses is left holding a token the network has already thrown away, and that
  account is disconnected until the person signs in again.
- **Updates the same shape either way** - pushed where a network supports it,
  found by checking on a timer where it does not.
- **Helpers for Django, FastAPI and Flask.** Django works on ordinary
  synchronous views and lets you write your storage as plain Django ORM code.
- **A test kit** so a network you write yourself can prove it behaves like
  the ones here. You do not need a pull request to this repository to add a
  network; publish a package, and socialchimp finds it.

### Getting started

```bash
pip install socialchimp
```

Then [docs/getting-started.md](docs/getting-started.md), which goes from
nothing to a post on Mastodon in six steps.

### Worth knowing

- **Facebook, Instagram, Threads, YouTube, TikTok, X and Pinterest all need
  you to create the app by hand**, and several review it before it works.
  That review is the slowest part of getting started, so begin it early.
  [docs/platforms.md](docs/platforms.md) says what each one needs.
- **Three networks have a trap that makes working code look broken.** An
  unaudited TikTok app posts everything as private. Pinterest on Trial shows
  your pins only to you. X answers 403 when your plan does not allow
  something. All three are called out where you will meet them.
- **The way platforms are written is now settled.** See
  [the promise about changes](docs/adding-a-platform.md#what-we-promise-about-changes).

### Quality

1725 tests, of which 19 skip because they need credentials for a network
that nobody has in CI. 100% of lines and branches covered, enforced - the
suite fails
below it. `mypy --strict` with no ignores anywhere. Checked on Python 3.11,
3.12 and 3.13.
