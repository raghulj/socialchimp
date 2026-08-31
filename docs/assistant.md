# For a coding assistant

This page is written for a model generating socialchimp code, not for a
person browsing the site. It is dense on purpose. For prose written for
people, see [the tutorial](tutorial.md) and [getting started](getting-started.md).

**This describes socialchimp {{SOCIALCHIMP_VERSION}}.** socialchimp is
pre-1.0: a change to the middle version number can break something. Check
the version installed (`socialchimp.__version__`) against the version this
page names before trusting anything below, and prefer
[`networks.json`](https://raghulj.github.io/socialchimp/networks.json) over
guessing at a network's features, options or limits - it is generated from
the code, this page is not.

## The four things

- **A connection** (`Connection`) is one social account someone connected -
  an id, which network, a token, and anything else that network needs (a
  Facebook page id, a YouTube channel id). Your app saves it; socialchimp
  never does.
- **Storage** is five methods your own class provides -
  `get_connection`, `save_connection`, `delete_connection`, `get_app`,
  `save_app` - so socialchimp has somewhere to read and write connections
  and app credentials without owning your database. Start from
  `InMemoryStorage` and replace it later. A blocking storage (Django's ORM,
  a plain `psycopg` cursor) goes through `sync_storage`, unchanged, so it
  runs on a thread instead of blocking the event loop.
- **A platform** is one class per network (`MastodonPlatform`,
  `BlueskyPlatform`, ...) built with no arguments and holding no per-account
  state. It knows how to sign someone in, publish, renew a token, and
  report what the network allows. Never call a platform's own methods
  directly unless you are writing one - go through `SocialChimp` and
  `Account`, which check `Feature`s and limits first and keep tokens fresh
  for you.
- **An update** (`Update`) is one shape for "something happened" - a
  comment, a like, a new follower - whether the network pushed it to a URL
  of yours or socialchimp had to poll for it on a timer. Your handler code
  never learns which of those happened; `Dispatcher.deliver` calls every
  registered handler with the same `Update` either way.

`SocialChimp` ties these together. `sc.account(connection_id)` returns an
`Account` bound to one connection; `account.post(...)`, `account.limits()`,
`account.check_state(...)`, `account.fetch_updates(...)` and
`account.delete_post(...)` are what an app calls day to day.

## The four sign-in shapes

A sign-in is a sequence of `LoginStep` values, always ending in `Finished`.
Match on the type you get back rather than assuming which one:

- **`SendToNetwork`** - the ordinary case. Redirect the person to `.url`;
  they come back through your `redirect_uri` and you call
  `sc.finish_login(...)` with what the network sent back.
- **`AskForDetails`** - for a network with no sign-in page to redirect to.
  Bluesky is the only one built in: there is no developer portal and no app,
  a person signs in with their handle and an app password they made
  themselves, and `.fields` says what to show them in a form of your own.
  `LoginRequest.app` is `None` on this path, and nothing is read from or
  saved to storage before it - a platform that takes this path lists
  `Feature.NEEDS_NO_APP`, and one that does not is refused without stored
  app credentials exactly as before.
- **`ChooseAccount`** - a pause. The person approved your app, but the
  network has several things it could mean (a Facebook page, an Instagram
  business account, a YouTube channel) and `.options` lists them. Show the
  choice, then call `sc.choose(...)` with the id they picked and
  `.resume_token` handed straight back - treat that token as a secret, the
  same as `SendToNetwork.remember`, because on some networks it carries the
  actual credentials.
- **`Finished`** - done. `.connection` is what you save.

`sc.create_app(...)` is a separate, earlier step some networks need before
any of this - only Mastodon can do it today (`Feature.CREATE_APP`); every
other network is registered by hand in a developer portal, and
`sc.create_app(...)` against one of those raises `NotSupportedError` naming
where to go instead, rather than pretending. A platform having a
`create_app` method is not proof it works - Facebook, Instagram and Threads
all have one, and it always raises, on purpose, so a call that reaches it by
mistake gets a real explanation instead of `AttributeError`. `Feature.CREATE_APP`
is the fact to trust, not whether the method exists; `/networks.json` already
makes that distinction for you.

## socialchimp raises. Your app handles.

Every problem comes back as an exception raised at the call that caused it,
never written down and handed back as data. Catch `SocialChimpError` to
catch everything:

| Error | What it means |
|---|---|
| `ConfigError` | Something is wrong on your side - a bad platform name, missing credentials, a datetime with no timezone. Also a `ValueError`. Fix the code; do not retry. |
| `InvalidPostError` | The post itself breaks a rule - too long, too many pictures, empty, a picture socialchimp could not read. Also a `ValueError`. Caught before sending where possible, so this is usually not a wasted request. |
| `NotSupportedError` | The network genuinely cannot do this - Bluesky cannot schedule, YouTube has no text-only post. Not a gap in socialchimp. `.suggestion` often says what to do instead. |
| `AuthError` | The network would not accept the token. Usually means the person has to connect their account again. |
| `TokenExpiredError` | An `AuthError`: renewal was tried and failed (no refresh token, or it is itself expired/revoked). |
| `NotAllowedError` | The account is real but lacks a permission. Ask for the right scope next time someone connects. |
| `NotFoundError` | The post, account or page asked for does not exist. |
| `RateLimitError` | The network wants you to slow down. `.retry_after` gives seconds, when the network says. |
| `NetworkError` | The network was never reached - a timeout, a dropped connection - after socialchimp already retried. Worth retrying later, unlike the others. |
| `SignatureError` | An incoming webhook request did not check out. Answer 401 and do nothing else with it. |
| `PlatformError` | The network answered with an error socialchimp has no better name for yet. `.raw` holds what it actually said. |

Two error types deliberately double as `ValueError` (`ConfigError`,
`InvalidPostError`) because they are raised for a bad value your own code
handed in, not for anything a network said - see the comment above
`ConfigError` in `socialchimp/errors.py`.

## Posting to several accounts is your loop, not socialchimp's

There is no method that posts to more than one account. Write the loop:

```python
from socialchimp import SocialChimpError

for connection_id in (mastodon_id, bluesky_id):
    try:
        result = await sc.account(connection_id).post(post)
    except SocialChimpError as failed:
        print("failed:", connection_id, failed)
    else:
        print("posted:", result.url)
```

**`SocialChimp.post_to_many` (with `PostJob` and `PostError`) was removed in
0.3.0 and has no replacement call.** A model trained on older material may
reach for it; it does not exist. Whether one account failing should stop the
others, whether a failure belongs in a row for a worker to retry, whether
someone needs telling - only the app calling socialchimp knows, so that
decision was moved into the loop above rather than picked for you.

## Also changed recently (0.3.0)

- **`Dispatcher.deliver` now raises.** It used to log a failed handler and
  mark the update handled anyway, so a handler that always failed silently
  lost every update after one log line. Now: every handler still runs, but
  the update is marked handled only if *all* of them succeeded, and any
  that raised come back together as an `ExceptionGroup` (`except*
  Exception:` to handle it). A webhook route (Django, FastAPI, Flask) whose
  handlers all fail now answers 500, not `200 {"ok": true}`, so the network
  retries an update that was never actually recorded as handled.
- **`Routes(..., secrets={...})` with no `deliver` now raises `ConfigError`
  at construction.** It used to accept this silently and quietly drop every
  correctly signed webhook.
- **A webhook route's own setup mistakes (`ConfigError`) now reach your
  error handling instead of being turned into a 500 with a JSON body.** A
  missing secret or an app that was never registered is a bug to fix, not a
  network condition to retry.
- **`SocialChimpError` actually catches everything now.** Before 0.3.0,
  `socialchimp.models` raised a bare `ValueError` for things like an empty
  `Post`, a `datetime` with no timezone, or an unreadable `Media` - all of
  which walked straight past `except SocialChimpError`. Those are now
  `InvalidPostError` or `ConfigError`, and being also a `ValueError` means
  old code that caught `ValueError` still works.

The [changelog](changelog.md) has the full 0.3.0 section, including changes
narrower than these; read it before writing code that has to match this
version exactly.
