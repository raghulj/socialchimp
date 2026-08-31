# socialchimp

[![CI](https://github.com/raghulj/socialchimp/actions/workflows/ci.yml/badge.svg?branch=dev)](https://github.com/raghulj/socialchimp/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/socialchimp?color=0b7285)](https://pypi.org/project/socialchimp/)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue)](https://pypi.org/project/socialchimp/)
[![Coverage](https://img.shields.io/badge/coverage-100%25-brightgreen)](CONTRIBUTING.md)
[![Types](https://img.shields.io/badge/types-mypy%20strict-blue)](pyproject.toml)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Licence](https://img.shields.io/badge/licence-MIT-black)](LICENSE)

One simple way to connect your app to social networks.

**Read this as a website:** **[raghulj.github.io/socialchimp](https://raghulj.github.io/socialchimp/)**
— the tutorial, the reference and everything below, searchable and easier to
read than markdown files in a repository.

**Mastodon · Bluesky · Facebook Pages · Instagram · YouTube · TikTok · X · Pinterest · Threads**

Sign people in, keep their tokens working, post for them, and hear about what
happens - a comment, a like - the same way whether the network pushes it to
you or socialchimp checks on a timer. One way to do this across all nine
networks, and direct access to any one of them when its own features are
what you need.

---

## What it does

- **Connect accounts.** Sign in with Mastodon, Bluesky, Facebook, Instagram,
  YouTube, TikTok, X, Pinterest or Threads.
- **Keep tokens working.** Refreshed before they expire, safely, even when
  several workers run at once.
- **Post and read.** Text, pictures, video. Read a post's numbers back where
  the network allows it.
- **Know what happened.** Comments and likes reach you the same way whether
  the network pushes them to you or socialchimp checks on a timer.
- **Get out of the way when you need more.** `account.direct` sends a
  request of your own through the same token, the same retries and the same
  rate limits - only the request is yours.

## What it does not do

- **It does not touch your database.** No models, no migrations. You write a
  small storage class, socialchimp hands you data to save. Your schema stays
  yours, and it works the same on Django, FastAPI, Flask, or nothing at all.
- **It does not post to several accounts in one call.** `account.post(...)`
  posts as one account and raises if that one fails. Looping over your
  accounts, and deciding what one failure means for the rest, is a few lines
  in your own app - only your app knows the right answer.
- **It does not pretend networks are the same.** Pinterest needs a board.
  Bluesky cannot schedule. YouTube has no text-only post. Where a network
  genuinely cannot do something, the answer says so by name instead of
  guessing.

## The real cost of starting

Two of the nine need nothing you have to build: socialchimp registers a
Mastodon app for you, and Bluesky has no app at all - just a handle and an
app password. The other seven - Facebook, Instagram, YouTube, TikTok, X,
Pinterest, Threads - need you to create an app by hand in that network's own
developer portal, and most of those review it before it works for anyone but
you. That review is usually the slowest part of getting started, so begin it
early. [Networks](docs/platforms.md) says what each one needs.

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

Once somebody has connected an account - the [tutorial](docs/tutorial.md)
covers signing them in - posting for them looks like this:

```python
from socialchimp import SocialChimp, Post

sc = SocialChimp(storage=my_storage)

account = sc.account(connection_id)
result = await account.post(Post(text="Hello from socialchimp"))
print(result.url)
```

`my_storage` is five methods you write once; `connection_id` is whichever
account you saved when that person signed in.

---

## Documentation

**New here?** Start with the [tutorial](docs/tutorial.md). It assumes you have
built a web app before but have never touched a social network API, and
explains why this is harder than one HTTP request.

**Learning**

- [Tutorial](docs/tutorial.md) - the ideas, and how the classes fit together
- [Getting started](docs/getting-started.md) - from nothing to a post, in six steps

**Three things people actually build**

- [A shop posting to its Facebook Page](docs/use-cases/facebook-django.md) -
  Django. Sign-in that pauses to ask which Page, and real scheduling.
- [A creator tool uploading video to TikTok](docs/use-cases/tiktok-fastapi.md) -
  FastAPI. Large files sent in pieces, and the audit trap that makes working
  code look broken.
- [Publishing YouTube Shorts](docs/use-cases/youtube-shorts-flask.md) - Flask.
  A network with no text-only post at all, and why you cannot ask for a Short.

**Reference**

- [Networks](docs/platforms.md) - what each one can do, and what it needs
- [Frameworks](docs/frameworks.md) - the ready-made routes in detail
- [Adding a platform](docs/adding-a-platform.md) - a network we do not support yet
- [Examples](examples/) - runnable programs, including two whole sample apps
- [Changelog](CHANGELOG.md) - what changed, and what it means for you

**Project**

- [Plan](docs/PLAN.md) - what is built and what is coming
- [Contributing](CONTRIBUTING.md) - the three rules that are not negotiable
- [Releasing](docs/releasing.md) - how a release goes out

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
