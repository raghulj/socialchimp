# socialchimp

[![CI](https://github.com/raghulj/socialchimp/actions/workflows/ci.yml/badge.svg)](https://github.com/raghulj/socialchimp/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/socialchimp?color=0b7285)](https://pypi.org/project/socialchimp/)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue)](https://pypi.org/project/socialchimp/)
[![Coverage](https://img.shields.io/badge/coverage-100%25-brightgreen)](CONTRIBUTING.md)
[![Types](https://img.shields.io/badge/types-mypy%20strict-blue)](pyproject.toml)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Licence](https://img.shields.io/badge/licence-MIT-black)](LICENSE)

One simple way to connect your app to social networks.

> **Status: 0.1.0, the first release.** All nine networks below work. The way
> platforms are written is now settled - see
> [the promise about changes](docs/adding-a-platform.md#what-we-promise-about-changes)
> before you write your own.

**Mastodon · Bluesky · Facebook Pages · Instagram · YouTube · TikTok · X · Pinterest · Threads**

---

## What it does

Your app needs to let people connect their social accounts, then post for
them and read what happens. Every network does this differently. socialchimp
gives you one way to do it, and gets out of the way when you need the network's
own features.

- **Connect accounts.** Sign in with Mastodon, Bluesky, Facebook, Instagram,
  YouTube, TikTok, X, Pinterest, Threads and more.
- **Keep tokens working.** Refresh them before they expire, safely, even when
  several workers run at once.
- **Post and read.** Text, pictures, video. Read posts and their numbers back.
- **Know what happened.** Comments and likes reach you the same way whether the
  network pushes them to you or we have to check on a timer.

## What it does not do

- **It does not touch your database.** No models, no migrations. You write a
  small storage class, socialchimp hands you data to save. Your schema stays
  yours, and it works the same on Django, FastAPI, Flask, or nothing at all.
- **It does not pretend networks are the same.** Pinterest needs a board.
  Bluesky cannot schedule. YouTube has no text-only post. Where networks
  differ, the API says so instead of guessing for you.

---

## Install

```bash
pip install socialchimp
```

Add your framework if you want the ready-made routes:

```bash
pip install "socialchimp[django]"    # or [fastapi], or [flask]
```

## A first look

```python
from socialchimp import SocialChimp, Post

sc = SocialChimp(storage=MyStorage())

# Post to a connected account.
account = sc.account(connection_id)
result = await account.post(Post(text="Hello from socialchimp"))
print(result.url)
```

Need something only that network can do? Same connection, direct access:

```python
await account.direct.post(
    "/api/v1/statuses",
    json={"status": "Hello", "visibility": "unlisted"},
)
```

Tokens, retries and rate limits are still handled for you. Only the request
is yours.

---

## Documentation

- [Getting started](docs/getting-started.md) - from nothing to a post
- [Networks](docs/platforms.md) - what each one can do, and what it needs
- [Frameworks](docs/frameworks.md) - ready-made routes for Django,
  FastAPI and Flask
- [Adding a platform](docs/adding-a-platform.md) - a network we do not
  support yet
- [Plan](docs/PLAN.md) - what is built and what is coming
- [Changelog](CHANGELOG.md) - what changed, and what it means for you
- [Examples](examples/) - runnable programs

## Contributing

Contributions are welcome. A few things to know first:

- **Tests come first.** Write the failing test, then make it pass.
- **Coverage must stay at 100%.** CI fails below it.
- **Types must be strict.** `mypy --strict` with no ignores.

```bash
git clone https://github.com/raghulj/socialchimp
cd socialchimp
uv sync --all-extras --dev
uv run pre-commit install --hook-type pre-commit --hook-type pre-push
uv run pytest
```

## Licence

MIT. See [LICENSE](LICENSE).
