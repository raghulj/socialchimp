# Getting started

This page takes you from nothing to a post on Mastodon, then shows how the
same code posts to Bluesky.

Mastodon comes first because it is the only network where socialchimp can
register your app for you. No developer portal, no waiting for approval.

## Install

```bash
pip install socialchimp
```

## Step 1 - somewhere to keep connections

socialchimp does not have a database. It hands you data and you decide where
it goes. That is five methods:

```python
from socialchimp import InMemoryStorage

storage = InMemoryStorage()
```

`InMemoryStorage` forgets everything when your program stops, which is fine
while you are trying things out. [Step 6](#step-6---real-storage) replaces it
with your own database.

## Step 2 - register your app

```python
from socialchimp import SocialChimp

sc = SocialChimp(storage=storage)

app = await sc.create_app(
    "mastodon",
    host="mastodon.social",
    name="My App",
    redirect_uri="http://localhost:8000/callback",
)
```

That is a real app on mastodon.social now, and socialchimp has saved its
credentials for you.

**Do this once per server.** Every Mastodon server is separate, so an app
registered on mastodon.social means nothing on fosstodon.org. socialchimp
keeps them apart for you, but you do have to register on each one.

Most networks cannot do this. Ask Facebook and you get a clear refusal
pointing at its developer portal, because Meta reviews every app by hand.

## Step 3 - sign someone in

Signing in has two halves, because the person goes off to the network in the
middle.

```python
step = await sc.start_login(
    "mastodon",
    host="mastodon.social",
    redirect_uri="http://localhost:8000/callback",
)

# step is SendToNetwork. Send the person to step.url.
# Keep step.remember with their session - you need it in a moment.
```

When they come back:

```python
step = await sc.finish_login(
    "mastodon",
    host="mastodon.social",
    redirect_uri="http://localhost:8000/callback",
    callback=dict(request.query_params),
    remember=session["socialchimp_remember"],
)

connection = step.connection  # already saved for you
```

### Why `remember` has to travel through your app

It holds a secret that proves the person who comes back is the same one who
left. socialchimp cannot hold it for you, because the two halves can happen
in different processes - your app might send someone away from one web worker
and get them back on another. Keeping it in memory works on your laptop and
fails in production.

Put it in the session, a signed cookie, or a row in your database.

### Some networks ask a question instead

`finish_login` can also come back with `ChooseAccount`. Facebook asks which
page, Instagram which business account, YouTube which channel:

```python
match step:
    case Finished(connection):
        ...  # done
    case ChooseAccount(options, resume_token):
        # Show options to the person, then:
        step = await sc.choose(
            "facebook",
            account_id=whichever_they_picked,
            resume_token=resume_token,
            redirect_uri="http://localhost:8000/callback",
        )
```

### Networks with no sign-in page

Bluesky has no page to send anyone to. You sign in with an app password, so
`start_login` answers with `AskForDetails` instead:

```python
step = await sc.start_login("bluesky", redirect_uri="unused")

# step.fields tells you what to ask for:
#   handle        "Your Bluesky handle"
#   app_password  "App password"  (secret=True - do not log it)
# step.help_url points at where the person creates one.

step = await sc.finish_login(
    "bluesky",
    redirect_uri="unused",
    callback={"handle": "someone.bsky.social", "app_password": "xxxx-xxxx"},
)
```

Show the fields in the order given, and never write anything marked `secret`
to a log.

## Step 4 - post

```python
from socialchimp import Post, Media

account = sc.account(connection.id)

result = await account.post(Post(text="Hello from socialchimp"))
print(result.url)
```

With a picture:

```python
await account.post(
    Post(
        text="Look at this",
        media=(Media.from_file("cat.png", alt_text="A cat asleep on a keyboard"),),
    )
)
```

The same two lines post to Bluesky. You do not write anything per network.

Your token is renewed before every post, so a post never fails just because
a token aged out. Bluesky's tokens last minutes, and you will not notice.

### Posting to several accounts at once

```python
job = await sc.post_to_many([mastodon_id, bluesky_id], Post(text="Hi"))

for result in job.succeeded:
    print("posted:", result.url)
for failure in job.failed:
    print("failed:", failure.connection_id, failure.error)
```

One account failing never hides the ones that worked.

## Step 5 - when a network cannot do something

socialchimp refuses rather than guessing:

```python
from socialchimp import NotSupportedError

try:
    await account.post(Post(text="Later", publish_at=tomorrow))
except NotSupportedError as refused:
    print(refused)  # "bluesky does not support scheduling posts."
```

Mastodon takes the same post happily. Ask before you post if you need to:

```python
from socialchimp import Feature

if Feature.SCHEDULE in sc.platform_for("bluesky").features:
    ...
```

Limits work the same way, and are looked up while running because they
genuinely change - a Mastodon server's post length is set by whoever runs it:

```python
limits = await account.limits()
print(limits.max_text_length)  # 500 on one server, 5000 on another
```

An over-long post is refused before anything reaches the network, so you get
a clear message instead of the network's error code.

## Step 6 - real storage

Swap `InMemoryStorage` for your own. Five methods, all `async`:

```python
class MyStorage:
    async def get_connection(self, connection_id): ...
    async def save_connection(self, connection): ...
    async def delete_connection(self, connection_id): ...
    async def get_app(self, platform, host): ...
    async def save_app(self, app): ...
```

Three things to get right:

- **`get_connection` returns `None` when there is nothing**, rather than
  raising.
- **`delete_connection` is quiet** when the connection is already gone.
  Retries happen.
- **`get_app` is keyed by platform *and* host.** Mastodon needs its own app
  per server, so `("mastodon", "mastodon.social")` and
  `("mastodon", "fosstodon.org")` are different rows.

`Connection` holds a token. Encrypt it at rest if you can.

### Running more than one process

Tokens are renewed under a lock so that two workers cannot renew the same
connection at once. That matters because Bluesky, Pinterest and TikTok
replace the refresh token every time it is used - the loser of a race ends up
holding a token the network has already thrown away, and that account is
disconnected until the person signs in again.

The default lock only works inside one process. If you run several, pass one
they share:

```python
from socialchimp import TokenManager

TokenManager(storage, platform.refresh, make_lock=my_redis_lock_for)
```

## Doing something socialchimp does not cover

Every network has features we have not modelled. You are not stuck:

```python
await account.direct.post(
    "/api/v1/statuses",
    json={"status": "Hello", "visibility": "unlisted"},
)
```

The token is still renewed, retries still happen, rate limits are still
respected. Only the request is yours.

## What next

- [Adding a platform](adding-a-platform.md) - a network we do not support yet
- [The plan](PLAN.md) - what is built and what is coming
