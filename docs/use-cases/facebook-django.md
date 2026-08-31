# Posting to a Facebook Page from the Django admin

A small business - call it Bench & Bloom - wants its staff to write an
announcement in the Django admin they already use, and have it appear on the
shop's Facebook Page, now or on Saturday morning. When somebody comments on
one of those posts, the team wants to know without watching Facebook.

New to socialchimp? Read the [tutorial](../tutorial.md) first. This page
assumes you know what a connection, a platform and an update are.

- [What we are building](#what-we-are-building)
- [What you need before you write any code](#what-you-need-before-you-write-any-code)
- [The code](#the-code)
- [Running it](#running-it)
- [What will go wrong, and why](#what-will-go-wrong-and-why)

---

## What we are building

Four pieces:

1. **A model and a storage class.** One table for connected Pages, written as
   ordinary Django ORM code and handed to `orm_storage`.
2. **Three views for connecting a Page.** Facebook's sign-in pauses in the
   middle to ask *which* Page, so there are three requests rather than two.
3. **An admin action.** Tick some announcements, choose "Post to Facebook" or
   "Schedule for later", done.
4. **A webhook view.** Facebook sends a request when somebody comments.

Everything here is sync WSGI Django. You do not need ASGI. socialchimp is
async underneath and `asgiref.sync.async_to_sync` bridges it, in one place,
which is what the Django helper already does for its own routes.

Runnable code for the socialchimp half of this is in
[`examples/facebook_django/`](../../examples/facebook_django/): `page_post_demo.py`
runs the whole sign-in, the scheduled post and the comment webhook against a
fake, with no Facebook app and no credentials, and `page_live.py` does the
same against real Facebook from the console.

> Everything under `examples/` is checked by `mypy --strict`, and Django ships
> no type information, so a file there that imported Django could not be
> checked. That is why the Django project itself is written out here rather
> than shipped as a runnable example. The socialchimp half is identical.

---

## What you need before you write any code

This is the slow part, and it is slow in a way that has nothing to do with
code. Start it today; write the code while you wait.

### 1. Create the app by hand

Meta has no way to register an app for you, and socialchimp says so if you
ask:

```python
await sc.create_app("facebook", name="...", redirect_uri="...")
# NotSupportedError: facebook does not support registering an app for you.
# Register it by hand in that network's developer portal, then save the id
# and secret you are given with Storage.save_app
```

So: go to <https://developers.facebook.com/apps>, create an app, add the
**Facebook Login** product to it, and put your callback address in **Valid
OAuth Redirect URIs**. It has to match what your code sends, character for
character - a trailing slash is a different address.

Take the **App ID** and **App Secret** from Settings → Basic. Those are the
`client_id` and `client_secret` your storage will hand back.

### 2. Wait for app review

The permissions this needs are all reviewed:

| Permission | What for |
|---|---|
| `pages_show_list` | See which Pages the person manages, so you can ask which one |
| `pages_read_engagement` | Read the Page, including comments |
| `pages_manage_posts` | Publish, schedule and delete |
| `business_management` | Pages owned by a business rather than a person - which is most Pages worth posting to |

socialchimp asks for all four by default. Until Meta's review passes, **they
work for people who have a role on the app in the portal and fail for
everybody else.** Add yourself and a colleague as testers and you can build
the whole thing; the first customer you hand it to will be turned away.

### 3. Get the business verified

Separately from app review, Meta wants documents about the company behind the
app - registration, an address, sometimes a phone call. This is the slowest
part of the whole project and there is nothing in your code that can hurry
it. Two to six weeks is normal.

**Say this out loud to whoever is paying for the work, on day one.** The
single most common way this project goes wrong is that the code is finished
in a week and then sits for a month, and nobody warned anybody.

### 4. A public address for the webhook

Facebook will not push to `localhost`. Use a tunnel while you are building,
and put the tunnel's address in both Meta's webhook form and your code.

While you are in that form, choose a **verify token**. It is any string you
invent; Meta quotes it back once when you save the URL, to prove the address
is yours. It is not the app secret, and mixing the two up is the usual reason
the form says the URL could not be verified without saying why.

---

## The code

### The model

Your table, your migration, your names. socialchimp never sees it.

```python
# shop/models.py
from django.db import models


class SocialAccount(models.Model):
    """One social account somebody has connected."""

    # socialchimp chooses this: Facebook's platform names a connection after
    # its Page, as "facebook:<page id>". Keep it as given - the webhook below
    # relies on it, because Meta tells you which Page something happened on
    # and nothing about which of your rows.
    id = models.CharField(primary_key=True, max_length=255)
    platform = models.CharField(max_length=50)
    host = models.CharField(max_length=255, null=True, blank=True)
    account_id = models.CharField(max_length=255)
    account_name = models.CharField(max_length=255)

    access_token = models.TextField()
    refresh_token = models.TextField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    scopes = models.JSONField(default=list)
    extra = models.JSONField(default=dict)

    connected_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.account_name} ({self.platform})"


class Announcement(models.Model):
    """Something the shop wants to say."""

    body = models.TextField()
    link = models.URLField(blank=True)
    page = models.ForeignKey(SocialAccount, on_delete=models.PROTECT)

    publish_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Leave empty to post immediately. Facebook needs at least "
        "10 minutes' notice and at most 75 days.",
    )
    facebook_post_id = models.CharField(max_length=255, blank=True)
    state = models.CharField(max_length=32, blank=True)
```

`access_token` is worth encrypting at rest. A page token that does not expire
is a page token that works forever for whoever reads your database.

### The storage class

Five methods, written the ordinary blocking way. No `async`, no base class,
no import of anything you have to inherit from.

```python
# shop/storage.py
from django.conf import settings
from socialchimp import AppCredentials, Connection, Token

from .models import SocialAccount


class Storage:
    """Where socialchimp's connections live. Ordinary Django ORM code."""

    def get_connection(self, connection_id: str) -> Connection | None:
        row = SocialAccount.objects.filter(pk=connection_id).first()
        # None, not an exception. "Not connected yet" is a normal state.
        if row is None:
            return None
        return Connection(
            id=row.pk,
            platform=row.platform,
            host=row.host,
            account_id=row.account_id,
            account_name=row.account_name,
            token=Token(
                access_token=row.access_token,
                refresh_token=row.refresh_token or None,
                expires_at=row.expires_at,
            ),
            scopes=tuple(row.scopes),
            extra=row.extra,
        )

    def save_connection(self, connection: Connection) -> None:
        # Called when a Page is first connected, and again after every token
        # renewal - so it replaces rather than inserts.
        SocialAccount.objects.update_or_create(
            pk=connection.id,
            defaults={
                "platform": connection.platform,
                "host": connection.host,
                "account_id": connection.account_id,
                "account_name": connection.account_name,
                "access_token": connection.token.access_token,
                "refresh_token": connection.token.refresh_token or "",
                "expires_at": connection.token.expires_at,
                "scopes": list(connection.scopes),
                "extra": connection.extra,
            },
        )

    def delete_connection(self, connection_id: str) -> None:
        # Quiet when it is already gone. Retries happen.
        SocialAccount.objects.filter(pk=connection_id).delete()

    def get_app(self, platform: str, host: str | None) -> AppCredentials | None:
        # Meta gives one app id and secret for the whole app, so these live
        # in settings rather than in a table. `host` is always None here -
        # it only matters for Mastodon, where every server is separate.
        if platform != "facebook":
            return None
        return AppCredentials(
            platform=platform,
            host=host,
            client_id=settings.FACEBOOK_APP_ID,
            client_secret=settings.FACEBOOK_APP_SECRET,
        )

    def save_app(self, app: AppCredentials) -> None:
        # Nothing to do. Only Mastodon registers apps through socialchimp;
        # Facebook's are typed into a portal by a human.
        pass
```

Two things to be careful about, because both are silent when wrong:

- **`expires_at` must have a timezone.** Set `USE_TZ = True` in your Django
  settings. socialchimp refuses a naive datetime at the door, because a naive
  one compares wrongly against an aware one and the failure is silent.
- **`refresh_token or None`.** Django's `TextField` gives you `""` for empty,
  and socialchimp wants `None`. An empty string reads as a refresh token that
  exists and does not work.

### Wiring it up

```python
# shop/social.py
from functools import cache

from socialchimp import SocialChimp
from socialchimp.contrib.django import orm_storage
from socialchimp.platforms.facebook import FacebookPlatform

from .storage import Storage

# Built here rather than reached for through `client().platform_for(...)`,
# which hands it back typed as the Platform protocol - and that has no
# `read_updates`, which the webhook below wants.
facebook = FacebookPlatform()


@cache
def client() -> SocialChimp:
    """The one SocialChimp for this process.

    Cached because the locks that stop two workers renewing the same token
    at once live on it, so a new one per request protects nothing.
    """
    return SocialChimp(
        storage=orm_storage(Storage()),
        platforms={"facebook": facebook},
    )
```

**Why `orm_storage` and not `sync_storage`.** Both take the same five
blocking methods. `sync_storage` runs them on any spare thread, which is
right almost everywhere and wrong on Django: Django keeps one database
connection per thread and a transaction belongs to the thread that opened
it. ORM code on a pool thread gets a second connection, outside the request's
transaction - it cannot see uncommitted writes, its own writes land in a
transaction nobody rolls back with the request, and it can deadlock against a
row the request has locked. `orm_storage` runs your methods back on the
thread the request arrived on.

`socialchimp.contrib.django.get_client()` builds the same thing from
`settings.SOCIALCHIMP = {"SYNC_STORAGE": "shop.storage.Storage"}`, and is
shorter. We build it by hand here only because the webhook wants a typed
handle on the Facebook platform.

### Connecting a Page: three requests, not two

This is the part that is different from every other network. The person
approves your app on Facebook, comes back - and you still do not know which
Page to post to.

```python
# shop/views.py
from asgiref.sync import async_to_sync
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import redirect, render
from socialchimp.platform import ChooseAccount, Finished, SendToNetwork

from .social import client

CALLBACK = "https://shop.example/social/callback/facebook"


@staff_member_required
def connect(request):
    """Request one: send the person to Facebook."""
    step = async_to_sync(client().start_login)(
        "facebook",
        redirect_uri=CALLBACK,
        state=f"staff-{request.user.pk}",
    )
    # Always SendToNetwork here; the match is for the day you add Bluesky,
    # which answers AskForDetails because it has no sign-in page at all.
    if not isinstance(step, SendToNetwork):
        raise RuntimeError(f"Unexpected step: {step!r}")

    # The second request needs both of these, and they cannot live in a
    # variable: the person may be sent away by one worker and come back to
    # another. The session is the obvious place on Django.
    request.session["fb_state"] = step.state
    request.session["fb_remember"] = step.remember
    return redirect(step.url)


@staff_member_required
def callback(request):
    """Request two: Facebook sent them back with ?code=..."""
    step = async_to_sync(client().finish_login)(
        "facebook",
        callback=request.GET.dict(),
        redirect_uri=CALLBACK,
        state=request.session["fb_state"],
        remember=request.session["fb_remember"],
    )

    # Facebook always stops here - even for somebody with one Page.
    if isinstance(step, ChooseAccount):
        # Never send this to the browser. It carries the person's own
        # Facebook token, because the code Facebook sent back can only be
        # swapped once and that already happened.
        request.session["fb_resume_token"] = step.resume_token
        return render(request, "shop/pick_a_page.html", {"options": step.options})

    raise RuntimeError(f"Unexpected step: {step!r}")


@staff_member_required
def choose(request):
    """Request three: they picked a Page."""
    step = async_to_sync(client().choose)(
        "facebook",
        account_id=request.POST["account_id"],
        resume_token=request.session.pop("fb_resume_token"),
        # All three requests use the same values the sign-in started with.
        redirect_uri=CALLBACK,
        state=request.session["fb_state"],
        remember=request.session["fb_remember"],
    )
    if not isinstance(step, Finished):
        raise RuntimeError(f"Unexpected step: {step!r}")

    # Already written through your save_connection. Nothing to save here.
    return redirect("admin:shop_socialaccount_change", step.connection.id)
```

```html
<!-- shop/templates/shop/pick_a_page.html -->
<h1>Which Page should we post to?</h1>
<form method="post" action="{% url 'shop:choose' %}">
  {% csrf_token %}
  {% for option in options %}
    <label>
      <input type="radio" name="account_id" value="{{ option.id }}">
      {{ option.name }} <small>{{ option.kind }}</small>
    </label>
  {% endfor %}
  <button type="submit">Connect</button>
</form>
```

**It asks even when there is one Page**, and that is deliberate. Choosing
silently would give your app two paths through the sign-in, one of which
almost never runs and so is never right - and the day somebody with two Pages
connects, they would find out which one you picked when a post appeared on
it.

### Posting from the admin

```python
# shop/admin.py
from datetime import UTC, datetime, timedelta

from asgiref.sync import async_to_sync
from django.contrib import admin, messages
from socialchimp import Post, PostState
from socialchimp.errors import SocialChimpError

from .models import Announcement, SocialAccount
from .social import client


def _publish(announcement: Announcement, when: datetime | None) -> None:
    """Send one announcement to its Page, now or later."""
    account = client().account(announcement.page_id)
    result = async_to_sync(account.post)(
        Post(
            text=announcement.body,
            publish_at=when,
            # `link` is the only setting Facebook takes here. Anything else
            # is refused before a request is spent on it.
            options={"link": announcement.link} if announcement.link else {},
        )
    )
    announcement.facebook_post_id = result.id
    announcement.state = result.state.name
    announcement.save(update_fields=["facebook_post_id", "state"])


@admin.register(Announcement)
class AnnouncementAdmin(admin.ModelAdmin):
    list_display = ("body", "page", "publish_at", "state")
    actions = ("post_now", "schedule")

    @admin.action(description="Post to Facebook now")
    def post_now(self, request, queryset):
        for announcement in queryset:
            try:
                _publish(announcement, None)
            except SocialChimpError as refused:
                # One clear sentence, already written for a person to read.
                self.message_user(request, str(refused), messages.ERROR)
            else:
                self.message_user(request, f"Posted: {announcement.facebook_post_id}")

    @admin.action(description="Schedule for the time on the announcement")
    def schedule(self, request, queryset):
        for announcement in queryset:
            when = announcement.publish_at
            if when is None:
                self.message_user(
                    request, "No publish_at on this one.", messages.WARNING
                )
                continue
            try:
                _publish(announcement, when)
            except SocialChimpError as refused:
                self.message_user(request, str(refused), messages.ERROR)
            else:
                # SCHEDULED, and result.url is None - Facebook has taken a
                # plan, not published a post, so there is nothing on the
                # Page to link to yet.
                self.message_user(request, f"Scheduled for {when:%d %b %H:%M}.")
```

**Facebook's scheduling is real.** Most networks have none - ask TikTok or
Bluesky to publish on Friday and socialchimp refuses rather than posting it
now. Facebook takes the post immediately and puts it out at the moment you
gave, and hands back `PostState.SCHEDULED`.

Two rules it enforces, and socialchimp checks both before sending anything:

- **Not less than ten minutes ahead.** An announcement scheduled for "two
  minutes from now" is refused with a message saying how far away it actually
  is.
- **Not more than 75 days ahead.**

```python
if Feature.SCHEDULE in client().platform_for("facebook").features:
    ...  # True on Facebook, False on TikTok and Bluesky
```

### The webhook

Meta sends a request to a URL of yours when somebody comments. Two different
things arrive at that address: a one-off `GET` when you first save the URL,
and a signed `POST` every time something happens.

```python
# shop/webhooks.py
from asgiref.sync import async_to_sync
from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from socialchimp import Dispatcher, InMemorySeenUpdates, Update, UpdateKind
from socialchimp.errors import SignatureError

from .models import Announcement
from .social import facebook

dispatcher = Dispatcher(seen=InMemorySeenUpdates())


async def someone_commented(update: Update) -> None:
    """Somebody left a comment on one of the shop's posts."""
    # update.connection_id is "facebook:<page id>", which is exactly the
    # primary key your SocialAccount row was saved under. No lookup table.
    #
    # `raw` is Meta's whole entry for the Page, not this one change - so the
    # comment itself is under changes[]. Read it defensively: this is
    # somebody else's JSON.
    for change in update.raw.get("changes", []):
        value = change.get("value", {})
        if value.get("item") != "comment":
            continue
        print(
            f"comment on {update.connection_id} "
            f"post {value.get('post_id')}: {value.get('message')}"
        )


dispatcher.on(UpdateKind.COMMENT_CREATED, someone_commented)


@csrf_exempt
def facebook_webhook(request):
    """Answer Meta's setup check, then receive its updates."""
    if request.method == "GET":
        try:
            challenge = facebook.answer_setup_check(
                request.GET.dict(),
                # The value you invented and typed into Meta's webhook
                # form. Not the app secret.
                verify_token=settings.FACEBOOK_VERIFY_TOKEN,
            )
        except SignatureError:
            # 403 is what Meta's own flow expects here.
            return HttpResponse(status=403)
        return HttpResponse(challenge, content_type="text/plain")

    try:
        # request.body is the bytes exactly as they arrived. Never
        # json.loads first - the signature is over these exact bytes, and
        # parsing the JSON and building it again changes the spacing and the
        # key order. That is the single most common reason a correct
        # signature appears to fail.
        facebook.check_signature(
            request.body,
            dict(request.headers.items()),
            # The app secret from Settings > Basic.
            secret=settings.FACEBOOK_APP_SECRET,
        )
    except SignatureError:
        # Say nothing about which check failed. That only helps whoever is
        # guessing.
        return HttpResponse(status=401)

    # read_updates, not read_update. Facebook batches when it is busy, and
    # being busy is exactly when you least want to drop the rest.
    for update in facebook.read_updates(request.body):
        async_to_sync(dispatcher.deliver)(update)

    return JsonResponse({"ok": True})
```

```python
# shop/urls.py
from django.urls import path

from . import views, webhooks

app_name = "shop"

urlpatterns = [
    path("social/connect/facebook", views.connect, name="connect"),
    path("social/callback/facebook", views.callback, name="callback"),
    path("social/choose/facebook", views.choose, name="choose"),
    path("social/webhooks/facebook", webhooks.facebook_webhook, name="webhook"),
]
```

`csrf_exempt` on the webhook only. Facebook has no way to send one of
Django's CSRF tokens, so a protected webhook answers 403 to everything and
Meta eventually stops trying. The three sign-in views are posted to by your
own pages and keep the protection - which is why the template above has
`{% csrf_token %}` in it.

**Those four views are exactly what
`socialchimp.contrib.django.urls()` gives you for free** - see
[frameworks](../frameworks.md). Written out here because the admin wants a
page for choosing a Page rather than a JSON body, and because it is worth
seeing once what the ready-made ones do.

---

## Running it

```bash
pip install "socialchimp[facebook,django]"
```

```python
# settings.py
USE_TZ = True

FACEBOOK_APP_ID = os.environ["FACEBOOK_APP_ID"]
FACEBOOK_APP_SECRET = os.environ["FACEBOOK_APP_SECRET"]
FACEBOOK_VERIFY_TOKEN = os.environ["FACEBOOK_VERIFY_TOKEN"]
```

```bash
python manage.py makemigrations shop && python manage.py migrate
python manage.py runserver
```

Then:

1. Open `https://shop.example/social/connect/facebook` as a staff user.
2. Approve on Facebook. **Tick the Page you want** on Facebook's own Page
   picker - leaving them all unticked is a valid thing to do there and gives
   you a sign-in with no Pages in it.
3. Pick the Page on your own page. It is connected.
4. Write an announcement in the admin, tick it, run "Post to Facebook now".
5. In Meta's dashboard, add a webhook for the **Page** object with the
   `feed` field, pointed at `https://shop.example/social/webhooks/facebook`,
   with your verify token. Comment on the post you just made.

Before any of that, try
`uv run python examples/facebook_django/page_post_demo.py`. It runs the
sign-in, the pause to choose a Page, a scheduled post and a comment webhook
against a fake, with no app and no waiting.

---

## What will go wrong, and why

### "It works for me and fails for the customer"

App review has not passed, or the business is not verified, or both. Every
one of the four permissions above is reviewed, and until then they work for
people with a role on the app in the developer portal and for nobody else.
There is nothing wrong with your code. See
[the section above](#2-wait-for-app-review).

### `AuthError: This person signed in, but there are no Facebook pages we can post to`

Two causes, and the message names both: either they manage no Pages, or they
left every Page unticked on Facebook's own picker while approving. The second
is much more common. Ask them to connect again and tick a Page.

### `InvalidPostError: Facebook will not schedule a post less than ten minutes ahead`

Exactly what it says, and socialchimp catches it rather than Facebook, so it
costs no request. If you are scheduling from a form, either make the minimum
selectable time fifteen minutes out or post immediately when the time is
already close.

### The webhook says "URL could not be verified" and nothing else

Meta will not say which check failed. Two things to look at:

- **The verify token in your settings is not the one in Meta's form.** They
  are two separate values, and the app secret is a third. `answer_setup_check`
  takes the verify token; `check_signature` takes the app secret.
- **Your address is not reachable.** Meta does that GET from the internet,
  not from your laptop.

### The signature never matches

Something parsed the body before the check. A signature is over the exact
bytes Facebook sent, and `json.loads` followed by `json.dumps` changes the
spacing and the key order. Read `request.body`, check it, parse afterwards.
This is the single most common cause and it looks like a wrong secret.

### The same comment is handled twice

Meta promises to deliver at least once, which is a promise to deliver twice
sometimes. socialchimp builds a stable id out of what Facebook said, so
giving `Dispatcher` a `SeenUpdates` - as above - drops the second copy. Use
something shared between your workers in production; `InMemorySeenUpdates`
is per process.

### A post with a picture and a video is refused

Facebook has no kind of post that carries both. Send two.

### A video over a gigabyte is refused

Facebook wants anything larger in pieces across several requests, and
socialchimp does not do that yet. The refusal says so rather than uploading
half of it. Send something smaller, or put it up another way.

### The token never seems to expire

It does not. Signing in gives a token for the *person*, good for about sixty
days; the token socialchimp saves is the *Page's*, taken from it, and a Page
token made that way has no expiry at all. So `refresh` usually has nothing to
do. There is no refresh token anywhere in Meta - a token is extended while it
still works or it is gone, which is why socialchimp renews early rather than
on the way past.

If somebody does get `TokenExpiredError`, the fix is always the same: they
connect the Page again.

### Two workers, one account

If you run more than one process, give socialchimp a lock they share. It
matters less on Facebook than on TikTok - Facebook has no refresh token to
lose - but the day you add a second network it matters a great deal. See
[getting started](../getting-started.md#running-more-than-one-process).

---

## Elsewhere

- [Networks](../platforms.md#facebook-pages) - the short list of what
  Facebook can and cannot do here.
- [Frameworks](../frameworks.md#django) - the ready-made routes.
- [TikTok from FastAPI](tiktok-fastapi.md) and
  [YouTube Shorts from Flask](youtube-shorts-flask.md) - the other two use
  cases.
