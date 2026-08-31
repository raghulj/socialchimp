# socialchimp

One simple way to connect your app to social networks.

Your app needs to let people connect their social accounts, then post for
them and read what happens. Every network does this differently - a
different sign-in flow, a different way tokens die and get renewed, a
different idea of what "300 characters" even means. socialchimp gives you
one way to do all of it, and gets out of the way when you need a network's
own features.

<ul class="sc-networks">
<li>Mastodon</li>
<li>Bluesky</li>
<li>Facebook Pages</li>
<li>Instagram</li>
<li>YouTube</li>
<li>TikTok</li>
<li>X</li>
<li>Pinterest</li>
<li>Threads</li>
</ul>

It does not touch your database - no models, no migrations. You write a
small storage class, socialchimp hands you the data to save, and your schema
stays yours. It does not pretend the networks are the same, either: Pinterest
needs a board, Bluesky cannot schedule, YouTube has no text-only post. Where
a network genuinely cannot do something, the answer says so by name instead
of guessing.

## One example

Once somebody has connected an account - the [tutorial](tutorial.md) covers
signing them in - posting for them looks like this:

```python
from socialchimp import SocialChimp, Post

sc = SocialChimp(storage=my_storage)

account = sc.account(connection_id)
result = await account.post(Post(text="Hello from socialchimp"))
print(result.url)
```

`my_storage` is five methods you write once; `connection_id` is whichever
account you saved when that person signed in. Need something only Mastodon
can do? `account.direct.post(...)` sends a request of your own through the
same token, the same retries, and the same rate limits.

## Where to go next

<div class="sc-next">

<a href="tutorial.md">
<strong>Learn it</strong>
<span>Never touched a social network's API before? The tutorial explains
what a connection, a platform and an update are, and why this is harder
than one HTTP request.</span>
</a>

<a href="use-cases/facebook-django.md">
<strong>Build something</strong>
<span>Three real apps, start to finish: a Facebook Page from Django, TikTok
video from FastAPI, and YouTube Shorts from Flask.</span>
</a>

<a href="platforms.md">
<strong>Look something up</strong>
<span>What each network can do, what it needs from you first, and the full
<a href="api/index.md">API reference</a> generated from the source.</span>
</a>

</div>

## Install

```bash
pip install socialchimp
```

Add a framework if you want the ready-made sign-in and webhook routes:

```bash
pip install "socialchimp[django]"    # or [fastapi], or [flask]
```

See [Frameworks](frameworks.md) for what those routes do, and
[Getting started](getting-started.md) for the ten-minute version that goes
from nothing to a real post on Mastodon.
