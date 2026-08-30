# socialchimp

One simple way to connect your app to social networks.

> **Status: early.** Being built in the open. The shared pieces are landing
> first, then Mastodon, then the rest. See [docs/PLAN.md](docs/PLAN.md) for
> what is done and what is next.

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

- [Plan](docs/PLAN.md) - what we are building and in what order
- Getting started - *coming soon*
- Adding a platform - *coming soon*

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
