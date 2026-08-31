# A Flask app on all nine networks

A small but real Flask app you can clone, run and read. It signs people in to
every network socialchimp covers, posts to as many of them at once as you
tick, receives what the four networks that push send it, and keeps all of it
in a sqlite file whose schema is entirely its own.

It exists because the difficulty of this job is not the posting. It is that
nine networks disagree about almost everything - how you sign somebody in,
whether they have to pick which account, whether a post of words alone is
allowed at all - and most of the disagreements are invisible until
production. So every one of them is on a page here rather than averaged away.

## Run it with no credentials

```bash
uv run --with flask python -m examples.flask_project.check_it_runs
```

That builds the whole app against `socialchimp.testing.FakePlatform` - a
network that works without a network - and walks a sign-in of each of the
four shapes, a post to four accounts with two of them refusing for two
different reasons, Meta's setup check right and wrong, a signed webhook and
an unsigned one, and somebody revoking the app. Nothing leaves the machine
and nothing needs an account. It prints a line per step.

The last section of it checks every option name this app can produce against
the `POST_OPTIONS` the platform file itself declares, so the example cannot
quietly teach a name that no longer exists.

## Run it for real

```bash
cp examples/flask_project/.env.example .env
# fill in the networks you want
set -a && . ./.env && set +a
uv run --with flask flask --app examples.flask_project run
```

Then open <http://localhost:5000>. Nothing has to be filled in to start: a
network you have set no credentials for says so on the connect page rather
than half-working.

Two of the nine work this afternoon. Mastodon needs nothing at all -
socialchimp registers the app on whichever server the person names, once per
server. Bluesky has no developer portal either; people sign in with an app
password.

`localhost` is fine for those two. For everything else you need an address a
network can reach, because it has to send people back to your callback and,
for four of them, push webhooks to it. Put a tunnel's address in `PUBLIC_URL`
and register that same address in the network's portal, character for
character.

## What each network needs first

| Network | Your app | Signing in | Words alone | Schedule | Updates |
|---|---|---|---|---|---|
| Mastodon | socialchimp registers it, per server | redirect | yes | yes | on a timer |
| Bluesky | nothing to register | handle + app password | yes | no | on a timer |
| Facebook Pages | by hand, **reviewed** | redirect, then asks which Page | yes | yes | pushed |
| Instagram | by hand, **reviewed** | redirect, then asks which account | **no** | no | pushed |
| YouTube | by hand, **reviewed** | redirect, then asks which channel | **no** | yes | on a timer |
| TikTok | by hand, **audited** | redirect | **no** | no | pushed |
| Threads | by hand, **its own app id and secret** | redirect | yes | no | pushed |
| X | by hand, **paid plan** | redirect | yes | no | on a timer |
| Pinterest | by hand, **reviewed** | redirect | **no** | no | none at all |

Being honest about the top of that column: **seven of the nine need an app
you create by hand in a developer portal, and five of those are reviewed by a
human before they work for anybody but you.** Meta also wants documents
proving the company behind the app is real. That review is measured in weeks.
Start it before you need it, not when you are ready to ship.

Three of them will lie to you politely in the meantime, which is worth
knowing before you spend a day on it:

- **TikTok**, until it has audited your app, forces every post to `SELF_ONLY`
  whatever privacy level you ask for, and answers that it worked. The video
  is there when its author looks at their own profile and nowhere else.
- **Pinterest**, on Trial access, hands back real pin ids for pins nobody but
  you can see. No field anywhere in the API says which tier you are on.
- **X** answers `403 client-not-enrolled` when your plan does not cover
  something, which reads exactly like a scope you forgot to ask for.

## The addresses

| Method | Address | What it is |
|---|---|---|
| `GET` | `/` | Connected accounts, and the last few things that happened |
| `POST` | `/connections/<id>/disconnect` | Forget one |
| `GET` | `/connections/<id>/limits` | What that network can do and currently allows |
| `GET` | `/sign-in/` | The nine, and what each needs first |
| `GET` | `/sign-in/start/<platform>` | Begin. `?host=` for Mastodon |
| `GET POST` | `/sign-in/callback/<platform>` | Where the network sends them back |
| `POST` | `/sign-in/details/<platform>` | The app-password form, for Bluesky |
| `POST` | `/sign-in/choose/<platform>` | Which Page, channel or account |
| `GET POST` | `/compose` | Write one post, send it to several accounts |
| `GET` | `/posts/<id>/<post_id>/state` | Ask YouTube or TikTok how a video is doing |
| `GET` | `/how-long?text=...` | The same words counted four different ways |
| `GET` | `/webhooks/<platform>` | Meta's one-off setup check |
| `POST` | `/webhooks/<platform>` | What the four that push send |

`/how-long` is the one worth trying first. A family emoji is one grapheme,
seven characters, eleven UTF-16 units and twenty-five UTF-8 bytes, and four
of the nine networks count in something other than characters. socialchimp
counts the way each of them does; that address only exists so you can build a
character counter that agrees with it.

## The four sign-in shapes

Every network is one of these, and an app that handles all four works with
the ones it has not added yet. All four are in `views/signin.py`, one branch
each.

1. **The ordinary redirect.** Mastodon, Facebook, YouTube, X, Pinterest,
   Threads, Instagram and TikTok. Send them to the network, they come back
   with a code, socialchimp swaps it.
2. **A handle and an app password.** Bluesky. There is nowhere to send
   anybody, so `start_login` answers with the fields to ask for instead of a
   link. Show one box per field, hide the ones marked `secret`, and never log
   them.
3. **A pause to ask which one.** Facebook, Instagram and YouTube.
   `finish_login` answers `ChooseAccount` instead of finishing, and a third
   request to `sc.choose(...)` finishes the job. It asks even when there is
   only one Page, on purpose - two code paths, one of which almost never
   runs, is two code paths one of which is never right.
4. **socialchimp registering the app for you.** Mastodon. `sc.create_app(...)`
   once per server, because every Mastodon server is separate.

## The files, in the order they are worth reading

```
config.py       what comes out of the environment, and why Meta needs two
                pairs of credentials rather than one
networks.py     the nine networks, in one table, read by the pages and by
                the code that builds a post
db.py           four tables, all of them this app's own
storage.py      the five methods socialchimp calls, as ordinary sqlite
login_notes.py  where a half-finished sign-in waits, and why not in a cookie
posts.py        turning one form into the post each network will take
views/          four blueprints: connections, sign-in, posting, webhooks
factory.py      the application factory that ties them together
templates/      plain Jinja, no build step, no CSS framework
check_it_runs.py  all of the above, against pretend networks
```

## Things worth knowing before you copy this

**Storage is blocking, and that is the normal case.** The five methods in
`storage.py` are ordinary synchronous sqlite. `sync_storage` wraps them so
the core can await them, running each on a spare thread. Django is the one
framework where that is the wrong wrapper - it has `orm_storage`, which runs
your ORM code back on the request's own thread, because a Django transaction
belongs to the thread that opened it.

**`save_connection` runs far more often than you expect.** Once when an
account is connected, and again after every token renewal. It has to replace,
not insert. On TikTok, Bluesky and Pinterest the old refresh token stops
working the moment a new one is handed out, so a renewal that never reaches
your table disconnects the account for good.

**The webhook reads raw bytes.** `request.get_data()`, not
`request.get_json()`. A signature is over the exact bytes that were sent, and
parsing the JSON and building it again changes the spacing and the key order.
That is the single most common reason a correct signature appears to fail.

**Posting to several accounts is this app's own loop.** There is no
`post_to_many`, deliberately. Whether TikTok refusing should stop the Facebook
post is a decision only your app can make. The loop in `views/posting.py`
chooses to carry on and to write every outcome down; deleting one `try` makes
it stop at the first failure instead.

**socialchimp raises, this app decides.** Every route catches
`SocialChimpError` and does something visible - a message on the page, a row
in `activity`. There is deliberately no catch-all error handler turning
refusals into an anonymous 500, because that would be the app declining to
decide.

**`run()`, not `asyncio.run()`.** Flask serves each request on a thread with
no event loop and socialchimp is async. `asyncio.run` per request would build
a loop, do the work and throw the loop away - and the HTTP connections
socialchimp pooled belong to that loop, so the next request finds a pool full
of sockets from a loop that no longer exists.
`socialchimp.contrib.flask.run` hands the work to one loop on one background
thread for the whole process. It is public exactly so your own views share it
with the ready-made routes.

**The ready-made blueprint exists too.** `socialchimp.contrib.flask.blueprint`
mounts the same four addresses and answers JSON. This app writes its own
because it wants HTML pages for choosing a Page and pasting an app password,
and every one of its routes is a few lines around the same `SocialChimp`
methods the blueprint calls.

## What this deliberately is not

An example, not a template to deploy. Before it were yours:

- **The tokens are stored in the clear.** Encrypting that column is a change
  to `storage.py` and to nothing else, which is the point of socialchimp
  never touching your database.
- **There are no CSRF tokens on the forms.** A real app puts them on
  everything except the webhook, which a social network has no way to sign
  with one.
- **There is no login of your own.** Anybody who can reach these pages can
  post as every connected account.
- **The default cookie key is a constant.** Set `FLASK_SECRET_KEY`.
- **Locks are per process.** Run more than one worker and give `SocialChimp`
  a `make_lock` built on something they share, or two of them can renew the
  same token at once - which on the networks that rotate refresh tokens
  disconnects the account.
- **Nothing is polled.** The five networks that cannot push are read with
  `Account.fetch_updates` and `socialchimp.events.Poller`, which want a
  worker of their own rather than a web request.

## Further reading

- [Tutorial](../../docs/tutorial.md) - the four ideas, slowly.
- [Networks](../../docs/platforms.md) - what each one can do and what bites.
- [Frameworks](../../docs/frameworks.md) - the ready-made routes.
