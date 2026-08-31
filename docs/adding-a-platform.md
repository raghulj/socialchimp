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
`create_app`, `delete_post`, `resume_login`, `fetch_updates`,
`check_signature`. There is no stubbing things out - a network that cannot
delete posts simply has no `delete_post`, and socialchimp asks before
calling.

## Rules that matter more than they look

**Only claim features you have.** `Feature` is what socialchimp trusts when
deciding whether to refuse something. Claiming `SCHEDULE` without honouring
`publish_at` means posts quietly going out immediately, which is worse than
an error.

**Say what your network actually cannot do.** If it has no scheduling, leave
`SCHEDULE` off and let socialchimp refuse with a clear message. Do not
approximate. A library that quietly does something else is a library nobody
can trust with a customer's account.

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
anything reaches the network, and scheduling on a platform without
`Feature.SCHEDULE` must raise `NotSupportedError` rather than quietly
posting.

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
