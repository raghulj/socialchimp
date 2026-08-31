# Frameworks

socialchimp works on its own. These helpers just save you writing the same
four routes again: start a sign-in, handle the callback, carry on after the
person picks which page to use, and receive what a network pushes to you.

They are convenience, never the only way in. Every route is a few lines
around a method on `SocialChimp` that you can call yourself, so your own
addresses, your own login checks, or a framework nobody has written a file
for are not special cases. The pieces they are built from live in
`socialchimp.contrib.shared`, and they have no framework in them at all.

**No models, no migrations.** Nothing here adds a table to your database or a
migration to your app, on any of the three. You write five storage methods
and your schema stays yours - that is the whole reason one library can serve
Django, FastAPI and Flask at once.

## Install

```bash
pip install "socialchimp[fastapi]"   # or [flask], or [django]
```

Importing `socialchimp` never imports any of them.

## The four addresses

Each helper mounts the same four, under whatever prefix you give it:

| Method     | Address                | What it does                                  |
|------------|------------------------|-----------------------------------------------|
| `GET`      | `connect/<platform>`   | Start a sign-in. Send the person here.         |
| `GET POST` | `callback/<platform>`  | The network sends them back here.              |
| `POST`     | `choose/<platform>`    | They picked which page or channel to use.      |
| `GET`      | `webhooks/<platform>`  | The network's one-off setup check.             |
| `POST`     | `webhooks/<platform>`  | Updates the network pushes to you.             |

`connect` takes an optional `state` (yours, handed back at the end so you know
whose account this is) and `host` (which server, for Mastodon).

---

## FastAPI

```python
import os

from fastapi import FastAPI
from socialchimp import (
    Dispatcher,
    InMemoryStorage,
    Post,
    SocialChimp,
    Update,
    UpdateKind,
)
from socialchimp.contrib.fastapi import router

# Swap InMemoryStorage for your own five methods before production.
sc = SocialChimp(storage=InMemoryStorage())

dispatcher = Dispatcher()


async def someone_commented(update: Update) -> None:
    print("comment on", update.connection_id, update.raw)


dispatcher.on(UpdateKind.COMMENT_CREATED, someone_commented)

app = FastAPI()
app.include_router(
    router(
        sc,
        redirect_uri="https://app.example/social/callback/{platform}",
        secrets={"facebook": os.environ["FACEBOOK_APP_SECRET"]},
        setup_tokens={"facebook": os.environ["FACEBOOK_VERIFY_TOKEN"]},
        deliver=dispatcher.deliver,
    ),
    prefix="/social",
)


@app.post("/posts")
async def write(connection_id: str, text: str) -> dict[str, str | None]:
    result = await sc.account(connection_id).post(Post(text=text))
    return {"id": result.id, "url": result.url}
```

- **Connect an account.** Send someone to
  `/social/connect/facebook?state=user-42`. They come back to
  `/social/callback/facebook`, which answers
  `{"step": "connected", "connection_id": ...}` - or `{"step":
  "choose_account", ...}`, in which case post the id they picked to
  `/social/choose/facebook` as `state` and `account_id`.
- **Post.** `POST /posts?connection_id=...&text=hello`.
- **Receive updates.** Point Meta at
  `https://app.example/social/webhooks/facebook`. Its setup check hits the
  same address with a `GET`, and is answered for you.

FastAPI is async and so is socialchimp, so nothing here runs on a thread.

---

## Flask

```python
import os

from flask import Flask, request
from socialchimp import (
    Dispatcher,
    InMemoryStorage,
    Post,
    SocialChimp,
    Update,
    UpdateKind,
)
from socialchimp.contrib.flask import blueprint, run

sc = SocialChimp(storage=InMemoryStorage())

dispatcher = Dispatcher()


async def someone_commented(update: Update) -> None:
    print("comment on", update.connection_id, update.raw)


dispatcher.on(UpdateKind.COMMENT_CREATED, someone_commented)

app = Flask(__name__)
app.register_blueprint(
    blueprint(
        sc,
        redirect_uri="https://app.example/social/callback/{platform}",
        secrets={"facebook": os.environ["FACEBOOK_APP_SECRET"]},
        setup_tokens={"facebook": os.environ["FACEBOOK_VERIFY_TOKEN"]},
        deliver=dispatcher.deliver,
    ),
    url_prefix="/social",
)


@app.post("/posts")
def write() -> dict[str, str | None]:
    account = sc.account(request.args["connection_id"])
    result = run(account.post(Post(text=request.args["text"])))
    return {"id": result.id, "url": result.url}
```

The addresses and the flow are exactly the same as FastAPI's.

Flask serves each request on a thread with no event loop. The blueprint keeps
one event loop on one background thread for the whole process and hands every
request's work to it, rather than building and throwing away a loop each time
- which would throw away socialchimp's pooled connections with it. `run` is
that bridge, and it is public so your own views use the same loop.

---

## Django

Three files. First your storage, written as ordinary synchronous ORM code:

```python
# social/storage.py
from django.conf import settings
from socialchimp import AppCredentials, Connection

from .models import SocialAccount


class Storage:
    def get_connection(self, connection_id: str) -> Connection | None:
        row = SocialAccount.objects.filter(pk=connection_id).first()
        return row.to_connection() if row is not None else None

    def save_connection(self, connection: Connection) -> None:
        SocialAccount.objects.update_or_create(
            pk=connection.id,
            defaults=SocialAccount.fields_from(connection),
        )

    def delete_connection(self, connection_id: str) -> None:
        SocialAccount.objects.filter(pk=connection_id).delete()

    def get_app(self, platform: str, host: str | None) -> AppCredentials | None:
        return AppCredentials(
            platform=platform,
            host=host,
            client_id=settings.FACEBOOK_APP_ID,
            client_secret=settings.FACEBOOK_APP_SECRET,
        )

    def save_app(self, app: AppCredentials) -> None:
        pass  # only Mastodon needs this
```

`SocialAccount` is your model, in your app, with your migration. socialchimp
never sees it.

Then two lines of settings:

```python
# settings.py
SOCIALCHIMP = {"SYNC_STORAGE": "social.storage.Storage"}
```

Use `SYNC_STORAGE` for a class written the ordinary way, and `STORAGE` for one
whose five methods are already `async`. Exactly one of the two.

Then the routes:

```python
# urls.py
import os

from django.urls import include, path
from socialchimp import Dispatcher, Update, UpdateKind
from socialchimp.contrib.django import get_client, urls

dispatcher = Dispatcher()


async def someone_commented(update: Update) -> None:
    print("comment on", update.connection_id, update.raw)


dispatcher.on(UpdateKind.COMMENT_CREATED, someone_commented)

social = urls(
    get_client(),
    redirect_uri="https://app.example/social/callback/{platform}",
    secrets={"facebook": os.environ["FACEBOOK_APP_SECRET"]},
    setup_tokens={"facebook": os.environ["FACEBOOK_VERIFY_TOKEN"]},
    deliver=dispatcher.deliver,
)

urlpatterns = [path("social/", include(social))]
```

And posting from a view of your own:

```python
# views.py
from asgiref.sync import async_to_sync
from django.http import JsonResponse
from socialchimp import Post
from socialchimp.contrib.django import get_client


def write(request):
    account = get_client().account(request.POST["connection_id"])
    result = async_to_sync(account.post)(Post(text=request.POST["text"]))
    return JsonResponse({"id": result.id, "url": result.url})
```

Three things worth knowing:

- **You do not need ASGI.** The views are ordinary sync views and
  `asgiref.sync.async_to_sync` runs the async call for them, so a sync WSGI
  app works as it is. Under ASGI the same views still work.
- **Your ORM code stays on the request's thread.** `SYNC_STORAGE` is wrapped
  with `sync_to_async(..., thread_sensitive=True)`. Django keeps one database
  connection per thread and a transaction belongs to the thread that opened
  it, so running your ORM code anywhere else would give it a second
  connection outside the request's transaction - reading stale rows, writing
  into a transaction nobody rolls back, and deadlocking against a row the
  request has locked.
- **Only the webhook is exempt from CSRF.** A social network has no way to
  send one of Django's tokens. Your own forms posting to `callback` and
  `choose` keep the protection, so put `{% csrf_token %}` in them as usual.

---

## Things all three share

### The raw body reaches the signature check untouched

Every helper reads the request body as bytes - `await request.body()`,
`request.get_data()`, `request.body` - and hands those exact bytes to the
platform. A signature is over the bytes that were sent, so anything that
parses the JSON and builds it again changes the spacing and the key order and
breaks it. That is the single most common reason a correct signature appears
to fail. If you write your own webhook route, read the body first and parse it
afterwards.

A request that does not check out is answered `401` and nothing else happens
to it. The answer says only "Refused." - which check failed is not something
to tell whoever is guessing.

### Where a half-finished sign-in waits

Signing in is two requests, and the second one needs what the first was handed
- the secret half of a PKCE pair, which server the person named, and the
resume token if the network stopped to ask which page to use. All of it is
filed under the sign-in's `state`.

Left alone, that is kept in the process, which is fine while you are trying
things out and wrong in production: two web workers do not share it, so a
person sent away by one and returning to another is told their sign-in has
expired. Write three methods - `keep`, `look_up`, `forget` - over your session,
a Redis key with a short life, or a small table, and pass it as `memory`:

```python
router(sc, redirect_uri=..., memory=MyLoginMemory())
```

See `socialchimp.contrib.shared.LoginMemory`.

The resume token never reaches the browser. On some networks it has to carry
the tokens themselves, so a hidden form field would be handing them out.

### Blocking storage anywhere, not only Django

`sync_storage` takes a storage class written the ordinary blocking way and
hands back the `Storage` the core wants, running each call on a spare thread:

```python
from socialchimp import sync_storage

sc = SocialChimp(storage=sync_storage(MyBlockingStorage()))
```

It lives in `socialchimp.storage` and needs no framework at all, so an app
with none can use it too - see [getting started](getting-started.md#when-your-database-layer-is-not-async).
`socialchimp.contrib.shared` still re-exports it, so the older import goes on
working.

`socialchimp.contrib.django.orm_storage` is the same thing with Django's
thread rule applied, which is why Django gets its own name for it.

### Errors

Whatever a network or your settings does wrong, these routes answer rather
than raise:

| Error                                  | Status |
|----------------------------------------|--------|
| `SignatureError`, `AuthError`, `TokenExpiredError` | 401 |
| `NotAllowedError`                      | 403    |
| `NotFoundError`                        | 404    |
| `InvalidPostError`, `NotSupportedError`| 400    |
| `RateLimitError`                       | 429, with `Retry-After` when the network said |
| `NetworkError`, `PlatformError`        | 502    |
| `ConfigError`, anything else           | 500    |

A failed setup check answers 403, because that is what Meta's own flow expects
there.
