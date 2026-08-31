# Examples

Runnable programs, checked by the same type checker as the library itself, so
they cannot quietly drift out of date.

## Two whole projects

Full applications covering **all nine networks** - signing in, posting, and
receiving updates. These are the ones to read if you are starting something.

- **[`django_project/`](django_project/)** - Django. A model, migrations,
  storage written as plain synchronous ORM code, and ordinary sync views.
- **[`flask_project/`](flask_project/)** - Flask. An application factory,
  blueprints, and storage over sqlite.

Both show the four shapes a sign-in can take, which is the thing that differs
between networks and where people get stuck:

| Shape | Networks | What your app does |
|---|---|---|
| Send them to the network | Mastodon, Facebook, Instagram, YouTube, X, Pinterest, Threads | Redirect, then handle the reply |
| Ask them for details | Bluesky | Show a form built from the fields the platform names |
| Ask which account | Facebook, Instagram, YouTube | Show the choices, then carry on |
| Register the app for them | Mastodon | Once per server, no portal, no waiting |

Each project has its own README and a `.env.example` naming every credential
and where it comes from. Each needs its own framework installed and nothing
else: `pip install "socialchimp[django]"`, `pip install "socialchimp[flask]"`.

## Nothing to set up

These need no credentials, no accounts and nothing installed but socialchimp
itself. They run against `socialchimp.testing.FakePlatform`, a network that
works without a network - ordinary classes that ask for no test framework.

- `post_to_each.py` - several accounts, one refusing, and the app carrying on.
  Posting to more than one network is your loop, on purpose.
- `failures_and_retries.py` - recording a failure in a table and retrying it
  later, which is the shape a real app needs.
- `facebook_django/page_post_demo.py` - a Facebook integration end to end
  against a pretend network.

## Against a real network

Each says what it needs at the top. See
[what each network needs](../docs/platforms.md).

- `post_to_mastodon.py` - the easiest to try, because Mastodon is the only
  network that lets us register the app for you
- `facebook_django/page_live.py` - post to a real Facebook Page
- `tiktok_fastapi/tiktok_app.py` - upload video from a FastAPI app
- `youtube_shorts_flask.py` - publish a Short from a Flask app

## Reading order

New to this? [The tutorial](../docs/tutorial.md) first, then
`post_to_each.py`, then whichever whole project matches your framework.
