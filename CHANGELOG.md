# Changelog

Notable changes, newest first. Versions follow
[semantic versioning](https://semver.org): while this is 0.x, a change to the
middle number may break something.

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
