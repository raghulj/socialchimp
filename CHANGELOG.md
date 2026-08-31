# Changelog

Notable changes, newest first. Versions follow
[semantic versioning](https://semver.org): while this is 0.x, a change to the
middle number may break something.

## Unreleased

- **`testing.FakePlatform` built with `accounts` now satisfies
  `CanResumeLogin`.** Before, a fake with accounts to choose between still
  had no `resume_login`, so calling `sc.choose(...)` against it raised
  `NotSupportedError` even though `finish_login` had just answered
  `ChooseAccount`. It now carries a real `resume_login`, the same as
  Facebook, Instagram and YouTube do, so `choose()` succeeds. If your own
  tests relied on that `NotSupportedError` to prove your app's error
  handling worked, they will now see the login finish instead.

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
