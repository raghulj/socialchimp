# Adding a platform

A platform is one Python class. This page shows what it has to provide and
how to check you got it right.

**You do not need to add it to this repository.** Publish your own package,
register it, and socialchimp will find it. That is the whole point of the
design: nobody has to wait for a pull request to be merged to use a network
they need.

## What a platform is

One class, built with no arguments, that never holds anything belonging to a
single account. One instance serves every account your app has.

Everything it needs for a particular account arrives as an argument: your
app's credentials on the `LoginRequest` and on `refresh`, the account on the
`Connection`. A platform never reaches into your storage for any of it.

Read [`src/socialchimp/platforms/mastodon.py`](../src/socialchimp/platforms/mastodon.py)
and [`bluesky.py`](../src/socialchimp/platforms/bluesky.py) alongside this
page. They are deliberately different from each other - Mastodon is one app
per server with a normal sign-in page, Bluesky is one address with an app
password - so between them they show most of the shapes you will meet.

## The seven things every platform provides

```python
from socialchimp.features import Feature, Limits
from socialchimp.platform import LoginRequest, LoginStep, SendToNetwork


class MyPlatform:
    name = "mynetwork"  # lowercase, no spaces
    features = Feature.POST_TEXT | Feature.REPLY  # only what it can do

    def api_base(self, connection) -> str:
        """Where this network's API lives."""

    def auth_headers(self, connection) -> Mapping[str, str]:
        """Headers proving we may act as this account. Cheap, no network."""

    async def limits(self, connection) -> Limits:
        """The numbers this network enforces right now."""

    async def start_login(self, request) -> LoginStep:
        """Begin signing someone in."""

    async def finish_login(self, request, callback, remember=None) -> LoginStep:
        """Carry on once the person comes back."""

    async def refresh(self, connection, app=None) -> Token:
        """Get a fresh token. `app` is your app's credentials."""

    async def publish(self, connection, post) -> PostResult:
        """Publish a post."""
```

Anything else is optional, and you say you can do it by having the method:
`create_app`, `delete_post`, `resume_login`, `check_state`, `fetch_updates`,
`check_signature`, `read_updates`, `answer_setup_check`. There is no stubbing
things out - a network that cannot delete posts simply has no `delete_post`,
and socialchimp asks before calling. Each one has a `Can...` protocol in
`socialchimp.platform` saying its exact shape, and a call on `Account` or
`SocialChimp` that finds it and refuses plainly when it is not there.

## Rules that matter more than they look

**Only claim features you have.** `Feature` is what socialchimp trusts when
deciding whether to refuse something. Claiming `SCHEDULE` without honouring
`publish_at` means posts quietly going out immediately, which is worse than
an error.

**Say what your network actually cannot do.** If it has no scheduling, leave
`SCHEDULE` off and let socialchimp refuse with a clear message. Do not
approximate. A library that quietly does something else is a library nobody
can trust with a customer's account.

**Say so if your network has no app to register.** Most do: somebody creates
one in a developer portal, saves the id and secret with `Storage.save_app`,
and socialchimp hands them to every sign-in. A network with no portal at all
has none of that, so list `Feature.NEEDS_NO_APP` and socialchimp asks storage
for nothing and hands your `start_login` a `LoginRequest` with `app=None`:

```python
features = Feature.NEEDS_NO_APP | Feature.POST_TEXT | Feature.REPLY
```

Bluesky is the built-in one - a person signs in with their handle and an app
password they made themselves. Leave the flag off for a network that does
need credentials, including one that can register itself: Mastodon creates
the app for you, but the app still exists, and a sign-in without it fails.
Claim it wrongly and your platform is handed `app=None` and finds out at the
token swap, which is a worse place to find out.

**Renewing a token is given your app's credentials.** Google, Meta and X all
sign a renewal with a client id and secret, and a platform has nowhere else
to get them - so `refresh` is handed them the same way a sign-in is.
socialchimp reads them out of your storage and passes them down. A network
that does not need them takes the argument and ignores it, the way Mastodon
and Bluesky do; a network that does need them raises `ConfigError` naming
`Storage.save_app` when none arrive.

**Big files are read a piece at a time.** `Media.size` says how many bytes
there are without opening the whole file, and `Media.piece(start, length)`
reads one piece off disk. Use both rather than `Media.read()` anywhere a
video could be large - YouTube, TikTok and Facebook all take video in
pieces, and reading it whole turns a four gigabyte upload into four
gigabytes of memory.

**`limits()` is looked up while running for a reason.** A Mastodon server's
post length is set by whoever runs it. Instagram counts down how many posts
are left today. Do not write these into your code as constants. Caching for
a few minutes is sensible.

**An unset limit is `None`, never `0`.** `0` means "none allowed". `None`
means "we do not know", and nothing is checked.

**Errors map onto ours.** Turn your network's errors into the ones in
`socialchimp.errors` so callers catch one thing. Compose with
`error_from_response` from `socialchimp.http` rather than writing your own
mapping from scratch.

**Use `HttpClient`.** Retries, `Retry-After`, rate limits and error mapping
are already written. Rolling your own means writing all of it again, worse.

## Shared helpers you should be using

These exist because nine platforms each wrote their own and the copies drifted.
Reach for them before writing a tenth.

**`rate_limit_from_headers` reads both spellings.** From `socialchimp.http`.
It knows `x-ratelimit-limit`, X's `x-rate-limit-limit` (one extra hyphen) and
the bare `ratelimit-limit`, and it copes with a header that lists every window
at once - Pinterest's `100, 100;w=1, 1000;w=60` - by taking the bare number in
front. `HttpClient` already calls it after every reply, so `http.rate_limit`
is filled in for you. If your network spells these some fourth way, add the
name to `socialchimp.http` rather than writing a reader in your platform file:
a private copy means `HttpClient.rate_limit` silently stays `None`, which
nothing anywhere complains about.

**`check_post` refuses a post of words on a network that has no such thing.**
Leave `Feature.POST_TEXT` off and a post with nothing attached is turned away
with a `NotSupportedError` naming what to attach instead. There is nothing to
write. If your network has one more thing worth saying, pass it as
`words_alone_advice=` and it is added to the message:

```python
check_post(
    post,
    platform=PLATFORM_NAME,
    features=self.features,
    limits=limits,
    words_alone_advice=(
        "Media.from_file('clip.mp4') will do it. Community posts are words, "
        "but they are not in YouTube's API at all."
    ),
)
```

**`check_option_names` refuses a setting your network never heard of.** From
`socialchimp.features`. It checks the *names* in `Post.options` against the
list you accept, and raises `InvalidPostError` before anything is sent:

```python
POST_OPTIONS: Final = ("visibility", "language")

check_option_names(post.options, platform=PLATFORM_NAME, allowed=POST_OPTIONS)
```

Pass `advice=` for one more sentence where the mistake has an obvious cause -
Pinterest uses it to say that `Post.text` is the pin's description, because
`description` is what people reach for first.

It deliberately does **not** check the values. What each one has to be is
different on every network - a web address on Facebook, true or false on
Instagram, one of four words on Mastodon - and the sentence explaining that is
the useful half of the message. Write that part yourself, right after.

**`NotSupportedError` takes a `suggestion=`.** `what` finishes the sentence
"yournetwork does not support ...", so keep it to a phrase. Everything else -
why, and what to do instead - goes in `suggestion` and reads as its own
sentences after it:

```python
raise NotSupportedError(
    platform=PLATFORM_NAME,
    what="replying to pins",
    suggestion=(
        "The Pinterest API has no comments in it at all, neither reading "
        "them nor writing them, so there is nothing to reply to."
    ),
)
```

**`Token.refresh_token_expires_at` is there if your network says.** Most do
not and it stays `None`. Pinterest's refresh token lasts sixty days and
nothing renews it, so an app that cannot see the date only finds out on the
day the account breaks. Fill it in where the network tells you, never guess
it, and `TokenManager` will then say plainly which of the two tokens ran out
instead of sending a doomed renewal to find out.

## What we promise about changes

The way a platform is written settled at 0.1.0, and it is now something you
can build against.

Writing the first nine networks changed it four times, and each change came
from a real network showing a gap: signing in had nowhere to carry a secret
between its two halves; platforms were reaching into storage for credentials
they should have been handed; direct access was guessing where a network
lives; and there was no sign-in step for networks with no page to send
anyone to. That was the right way to find those things, and the wrong thing
to keep doing once other people's platforms exist.

From here:

- **Adding something is a minor release.** A new `Feature`, a new
  `UpdateKind`, a new optional `Can...` extra, a new field on `Limits` with a
  default. Your platform keeps working and does not need touching.
  `Feature.NEEDS_NO_APP` arrived in 0.3.0 this way: a platform that does not
  list it behaves exactly as it did.
- **Changing or removing something is a major release**, and comes with a
  note saying what to do about it.
- **Anything named with a leading underscore is ours**, including
  `platforms/_meta.py`. It can change in any release.

Pin the contract in your own package the way a database driver pins its
library:

```toml
dependencies = ["socialchimp>=0.1,<0.2"]
```

Run `PlatformChecks` in your own tests and a change that affects you shows up
as a failing test with a message, rather than as somebody's post not arriving.

## Checking it behaves

Subclass `PlatformChecks` and you inherit a battery of checks:

```python
from socialchimp.testing import PlatformChecks


class TestMyPlatformBehavesLikeTheOthers(PlatformChecks):
    def make_platform(self):
        return MyPlatform()
```

Install it with `pip install "socialchimp[testing]"`.

These hold you to what you claim: a platform declaring it can create apps
must actually be able to, a post over a declared limit must be refused before
anything reaches the network, scheduling on a platform without
`Feature.SCHEDULE` must raise `NotSupportedError` rather than quietly
posting, a platform listing `Feature.NEEDS_NO_APP` must really start a login
with nothing saved, and a platform that pauses to ask which account to use
must have a `resume_login` socialchimp can actually call.

Add `make_connection()` and `make_transport()` to unlock the checks that need
a working platform. Leave them out and those skip with a line telling you
which method to add.

Add `make_post(text)` if your network wants more on a post than words -
YouTube refuses any video without a title, so nothing it will look at can be
built out of text alone. The checks that measure length build their posts
through it, so they measure the length rather than whatever else was
missing.

**These are a floor, not a ceiling.** They prove your platform is shaped
right. Only your own tests prove it talks to your network correctly.

### One respx trap worth knowing

The checks above are strict about a mistake costing no request, and the tests
you write yourself should be too - the pattern is to set a route up, expect a
refusal, and then assert nothing reached the network:

```python
with (
    respx.mock(base_url=API, assert_all_called=False) as network,
    pytest.raises(InvalidPostError),
):
    await platform.publish(connection, a_bad_post())

assert not network.calls
```

`assert_all_called=False` is doing real work there. `respx.mock()` defaults it
to `True`, which fails the test on the way out if any route you registered was
never called - which is exactly what a test proving nothing was sent has
arranged. Leave it out and the test fails with a respx complaint about an
uncalled route rather than telling you anything about your platform, and the
usual fix people reach for is to delete the route, which throws away the half
of the test that proves the refusal happened before the request rather than
after it.

## Letting socialchimp find it

In your package's `pyproject.toml`:

```toml
[project.entry-points."socialchimp.platforms"]
mynetwork = "my_package.platform:MyPlatform"
```

Install it and it works:

```python
sc = SocialChimp(storage=storage)
await sc.account(connection_id).post(Post(text="Hello"))
```

Nothing else to register. Platforms are loaded only when asked for, so
installing yours does not slow anything else down.

## Naming

`socialchimp-contrib-<network>` on PyPI, imported as
`socialchimp_contrib_<network>`. Nothing enforces this - it just makes yours
easy to find.

## If you would like it built in

Open an issue first so we can agree where it fits in the order - see
[the plan](PLAN.md). The rules in [CONTRIBUTING.md](../CONTRIBUTING.md)
apply: tests first, 100% coverage, strict types, plain words.
