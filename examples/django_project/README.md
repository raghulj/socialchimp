# A Django project that posts to all nine networks

A small Django site you can clone and run. It connects social accounts, shows
them in a list, and posts to as many of them as you tick — across every
network socialchimp covers.

It is here to be read as much as run. New to socialchimp? Start with the
[tutorial](../../docs/tutorial.md); this is the same ideas as a working
project rather than as snippets.

There is nothing to install beyond socialchimp and Django, no build step, no
CSS framework, and no credentials needed to see it work.

```bash
uv run python examples/django_project/manage.py migrate
uv run python examples/django_project/manage.py test social   # 12 tests, no network
uv run python examples/django_project/manage.py runserver
```

---

## What it does

- **Signs somebody in**, in all four of the shapes the nine networks come in.
- **Posts to several accounts at once**, in the app's own loop, with each
  network's own settings and each network's own refusals shown to the person.
- **Receives webhooks** from the four networks that push, with Meta's setup
  handshake and a real signature check.
- **Writes down every attempt** — the successes and the refusals — in a table
  of its own.

Two things it deliberately does not do: hide a network's refusal, and decide
on your behalf what a failure means.

---

## The layout

```
examples/django_project/
├── manage.py
├── .env.example              every credential, with where each one comes from
├── socialsite/               the project: settings, urls, wsgi, asgi
└── social/                   the one app
    ├── models.py             SocialConnection, SocialApp, PostAttempt
    ├── storage.py            the five methods, as plain Django ORM code
    ├── client.py             the one SocialChimp, and the sync/async bridge
    ├── networks.py           the nine networks, and what only this app knows
    ├── posting.py            one form -> nine different posts
    ├── views.py              the four sign-in shapes, and the posting loop
    ├── webhooks.py           the four networks that push
    ├── urls.py
    ├── tests.py              proof it all works, against fakes
    ├── migrations/
    └── templates/social/     six plain HTML pages
```

The project package is called `socialsite` rather than `config` so it does
not collide with every other example in this repository.

### Where to start reading

1. **`social/storage.py`** — five blocking methods over the ORM. This is the
   only thing socialchimp asks you for, and the reason it has no models of
   its own.
2. **`social/views.py`** — the four shapes of a sign-in, and the loop that
   posts to several accounts.
3. **`social/posting.py`** — what each network actually wants, and what this
   app refuses to guess.
4. **`social/webhooks.py`** — raw bytes, signatures, and the three different
   secrets people mix up.

---

## Running it

```bash
cd examples/django_project
cp .env.example .env          # then fill in the networks you want
python manage.py migrate
python manage.py runserver
```

Open <http://localhost:8000>. Every network is listed whether it is
configured or not; connecting one that is not says so plainly rather than
failing later.

**Start with Mastodon.** It is the only network you can be posting to in five
minutes: socialchimp registers the app for you, so there is no portal and
nothing to wait for. On its connect page, register the app on a server, then
connect.

**Then try Bluesky.** No app anywhere — the sign-in asks for a handle and an
app password, which is the second of the four shapes and the one people's
code usually has no branch for.

For the other seven, read the table below before promising anybody a date.

### Webhooks

The networks that push will not reach `localhost`. Run a tunnel, put its
hostname in `DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS`, and
point the network at:

```
https://<your tunnel>/social/webhooks/<network>
```

Meta's three ask a question first: a one-off `GET` carrying a challenge and
the verify token you invented. Get the token wrong and Meta says the URL
could not be verified, without saying why.

---

## The nine networks, and what each needs first

| Network | An app? | Reviewed? | Sign-in shape | Text alone | Pictures | Video | Schedule | Pushes |
|---|---|---|---|---|---|---|---|---|
| **Mastodon** | socialchimp makes it | no | redirect | yes | yes | yes | yes | on a timer |
| **Bluesky** | none exists | no | asks for details | yes | yes | no | no | on a timer |
| **Facebook Pages** | by hand | yes, **+ business verification** | redirect, **then asks which Page** | yes | yes | small only | yes | **yes** |
| **Instagram** | by hand | yes, **+ business verification** | redirect, **then asks which account** | **no** | yes, **from a web address only** | yes, from a web address | no | **yes** |
| **Threads** | by hand, **its own id and secret** | yes | redirect | yes | yes, from a web address | yes, from a web address | no | **yes** |
| **TikTok** | by hand | **audited, or every post is private** | redirect | **no** | no | yes | no | **yes** |
| **YouTube** | by hand | yes | redirect, **then asks which channel** | **no** | no | yes | yes | on a timer |
| **X** | by hand | **paid plan** | redirect | yes | yes | yes | no | on a timer |
| **Pinterest** | by hand | yes, **Trial pins are invisible** | redirect | **no** | yes | yes | no | **no** |

Being honest about that table: **seven of the nine need an app you create by
hand in a developer portal, and five of those seven are reviewed before they
work for anybody outside your own account.** Meta also wants documents about
the company behind the app, which takes two to six weeks and which nothing in
your code can hurry. Say that out loud on day one to whoever is paying for
the work — the usual way this goes wrong is that the code is finished in a
week and then sits for a month.

Three of them fail in a way that looks like success, which is worse than
failing:

- **TikTok, before its audit**, forces every post to `SELF_ONLY`. It answers
  that the post worked, the author sees it on their own profile, and nobody
  else ever will.
- **Pinterest, on Trial access**, makes real pins with real ids that nobody
  but their author can see. No field anywhere in the API says which tier you
  are on.
- **X, on the wrong plan**, answers 403 with `client-not-enrolled`, which
  reads exactly like a scope you forgot to ask for. socialchimp names this
  one on sight.

`.env.example` says where each credential comes from, one comment per
network. [docs/platforms.md](../../docs/platforms.md) is the longer version.

---

## What the code is actually showing you

### Storage, and why it is written blocking

socialchimp has no models, no migrations and no opinion about your schema. It
calls five methods. `social/storage.py` writes them as ordinary Django ORM
code — `Model.objects.filter(...)`, `update_or_create(...)` — with no `async`
and no base class to inherit from.

`orm_storage` is what makes that work, and it is not the same as
`sync_storage`. Both take the same five blocking methods; `sync_storage` runs
them on any spare thread, which is right everywhere except Django. Django
keeps one database connection per thread and a transaction belongs to the
thread that opened it, so ORM code on a pool thread gets a *second*
connection outside the request's transaction. `orm_storage` runs them back on
the thread the request arrived on.

### The four sign-in shapes

| Shape | Networks | What comes back |
|---|---|---|
| Send them to the network | all but Bluesky | `SendToNetwork` — redirect, and keep `remember` |
| Ask them for details | Bluesky | `AskForDetails` — show a box per field |
| Ask which account | Facebook, Instagram, YouTube | `ChooseAccount`, then `sc.choose(...)` |
| Done | all of them, eventually | `Finished` — already saved |

Two values travel between requests and neither can live in a variable:
`remember`, and `resume_token`. The two halves of a sign-in are separate
requests and can be answered by different web workers. Both are secrets, and
both go in the session — never in a URL, a hidden field, or a log.

### Posting to several accounts is the app's own loop

There is no `post_to_many`, and that is deliberate rather than missing.
socialchimp posts as one account at a time; when one fails it raises and
stops. Only your app knows whether TikTok refusing should stop the Facebook
post as well.

`_publish_to_each` in `views.py` is that loop. It chooses to carry on: every
account is tried, every result is written to `PostAttempt`, and every refusal
is shown. Deleting the `try` makes it stop at the first failure instead, and
both are one line of difference.

### A network that cannot do something is refused, visibly

Nothing in this project skips a network quietly or rewrites a post to make it
acceptable. Ask Bluesky to schedule and you get *"bluesky does not support
scheduling posts"* on the page. Send YouTube a video with no title and you get
a sentence explaining that `Post.text` is the description here. Hand Instagram
a local file and you get told it fetches files itself and this one needs to be
somewhere public first.

The one refusal the *app* makes rather than the library is handing a web
address to a network that uploads bytes — socialchimp answers that with a
plain `ValueError` from inside the upload rather than one of its own errors,
so a loop catching `SocialChimpError` would miss it.

### Finding accounts that need reconnecting

`Storage` has five methods and not one of them lists anything, on purpose. So
"which accounts are about to stop working?" is a query over your own columns:
`SocialConnection.objects.refresh_running_out()`. It only works because
`refresh_token_expires_at` is a real column and not a key inside a JSON blob.

That is the expiry worth acting on. An *access* token running out is handled
for you — socialchimp renews it before a post, under a lock, and writes the
new one back through `save_connection`. A *refresh* token running out is the
end of the line, and Pinterest's lasts sixty days.

### The webhook

Three different secrets, and mixing them up is the usual reason a webhook
fails silently:

| | What it is | Which call takes it |
|---|---|---|
| verify token | a string **you invented** and typed into the network's form | `answer_setup_check` |
| app secret | from the developer portal (TikTok: client secret) | `check_signature` |
| app id | not a secret at all | neither |

And the rule that matters more than any of them: **check the signature
against `request.body`, the raw bytes, before anything parses them.** Parsing
the JSON and building it again changes the spacing and the key order, and the
signature then fails on a message that was perfectly good. It looks exactly
like a wrong secret.

---

## Ready-made routes

`socialchimp.contrib.django.urls()` gives you `connect`, `callback`, `choose`
and `webhooks` in four lines. This project writes them out instead, because
it wants its own pages and its own messages — and because it is worth seeing
once what those four lines are doing.

```python
from socialchimp.contrib.django import get_client, urls

social = urls(
    get_client(),
    redirect_uri="https://app.example/social/callback/{platform}",
    secrets={"facebook": os.environ["FACEBOOK_APP_SECRET"]},
    setup_tokens={"facebook": os.environ["FACEBOOK_VERIFY_TOKEN"]},
    deliver=dispatcher.deliver,
)
```

---

## Elsewhere

- [Tutorial](../../docs/tutorial.md) — the ideas, explained.
- [Networks](../../docs/platforms.md) — what each one can do, and what bites.
- [Frameworks](../../docs/frameworks.md) — the ready-made routes.
- [A Facebook Page from the Django admin](../../docs/use-cases/facebook-django.md)
  — the same ground, one network deep, with an admin action instead of a form.
