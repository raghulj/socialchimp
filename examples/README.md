# Examples

Runnable programs, checked by the same type checker as the library itself, so
they cannot quietly drift out of date.

## Nothing to set up

These need no credentials and no accounts. Start here.

- `post_to_many.py` - one post to several networks, and what happens when one
  of them refuses
- `facebook_django/page_post_demo.py` - the whole shape of a Facebook
  integration against a pretend network: a database, signing in, choosing a
  Page, a scheduled post, and a signed webhook

## Against a real network

These need an account, and most need an app you created by hand. Each one says
what it needs at the top. See [what each network needs](../docs/platforms.md).

- `post_to_mastodon.py` - register an app, sign in, post. The easiest to try,
  because Mastodon is the only network that lets us register the app for you.
- `facebook_django/page_live.py` - post to a real Facebook Page
- `tiktok_fastapi/tiktok_app.py` - upload video from a FastAPI app
- `youtube_shorts_flask.py` - publish a Short from a Flask app

Each one has a walkthrough explaining it:
[Facebook](../docs/use-cases/facebook-django.md) ·
[TikTok](../docs/use-cases/tiktok-fastapi.md) ·
[YouTube Shorts](../docs/use-cases/youtube-shorts-flask.md)
