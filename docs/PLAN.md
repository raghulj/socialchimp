# Plan

This is the living plan for socialchimp. It changes as we learn things.
Every finished item links to the pull request or commit that did it.

Last updated: 2026-08-31

---

## What we are building

A Python library that lets your app connect to social networks, post to them,
read from them, and get told when something happens (a comment, a like).

Your app keeps its own database. socialchimp never creates tables and never
runs migrations. You give it a small storage class, it hands you back the
data to save.

## Who it is for

People building on Django, FastAPI, or Flask who need to connect user accounts
to social networks and post on their behalf.

---

## The three layers

We build in three layers. Each one works on its own, so we can ship the first
one before the others exist.

**1. Connections.** Sign a user in to a social network, get a token, keep that
token working. Refresh it before it expires. Tell your app when something
changed so it can save it.

**2. Posting and reading.** Send a post. Read posts back. Read the numbers
(likes, views). Upload pictures and video.

**3. Updates.** Find out when someone comments or likes. Some networks push
this to you. Some do not, so we check on a timer and tell you the same way.

On top of those sits **the shared way of doing things** - one `post()` call
that works on every network. It is built using only the public parts of the
three layers below it. If it ever needs something private, that means a lower
layer is missing a feature, and we add it there instead of taking a shortcut.

You can always go around the shared way and talk to a network directly, using
the same connection. Nothing is hidden from you.

---

## Rules we hold

These are not suggestions. CI fails if any of them break.

- **Tests first.** Write the failing test, then the code that passes it.
- **100% test coverage.** No line ships untested. `pytest` fails below 100%.
- **Everything typed.** `mypy --strict` passes with no ignores. If a type is
  hard to write, that usually means the design is wrong.
- **No lying about networks.** If Pinterest needs a board and Bluesky cannot
  schedule, our API says so plainly instead of pretending otherwise.
- **Plain words.** In code, docs, and error messages. If a name needs a
  glossary, pick a different name.
- **Small commits.** One idea per commit, with a message that says why.

---

## Platform order

We are building these in the order below. The first four are the priority.

| # | Platform | Why it is here | Status |
|---|----------|----------------|--------|
| 1 | Mastodon | Only network where we can create the app automatically. No approval needed, so anyone can try the library in five minutes. | Not started |
| 2 | Bluesky | No signup portal. Short-lived tokens, so it proves our token refresh works. | Not started |
| 3 | Facebook Pages | Highest demand. Doing the Meta login work here makes Instagram and Threads much cheaper. | Not started |
| 4 | Instagram | Posting is two steps: build the post, wait, then publish it. First network that needs the job model. | Not started |
| 5 | YouTube | Video and Shorts. Big uploads sent in pieces. | Not started |
| 6 | TikTok | Also uploads in pieces, with different rules. Tells us when publishing finished. | Not started |
| 7 | X / Twitter | Wide demand. Media upload still uses the older endpoint. | Not started |
| 8 | Pinterest | Every pin needs a board, so it proves we handle "where does this post go". | Not started |
| 9 | Threads | Same two-step posting as Instagram, but its own separate app and login. | Not started |
| - | Discord, Telegram, LinkedIn, Reddit, Tumblr | After the nine above. | Not started |

---

## Steps

### Step 1 - Set up the project
- [x] Repository, MIT licence, README
- [x] Testing, type checking, linting, coverage gate
- [x] Git hooks that check code before commit and push
- [x] CI on every push and pull request
- [ ] This plan, kept up to date

### Step 2 - The shared pieces
Everything a platform needs, before any platform exists.
- [ ] `Connection`, `Post`, `PostResult`, `Media` - the data we pass around
- [ ] `Storage` - the small class your app fills in
- [ ] Errors, one set for all networks
- [ ] `Feature` - what each network can and cannot do
- [ ] `Platform` - what a platform file must provide
- [ ] Finding installed platforms
- [ ] HTTP calls: retries, rate limits, paging
- [ ] Keeping tokens fresh, safely, when several workers run at once

### Step 3 - First platform, end to end
- [ ] Mastodon: create app, sign in, post, read, live updates
- [ ] A test kit other platforms can reuse to check they behave the same

### Step 4 - The rest of the platforms
In the order in the table above.

### Step 5 - Framework helpers
- [ ] Django, FastAPI, Flask: ready-made routes and storage examples

### Step 6 - Docs and examples
- [ ] Getting started, one page per platform, a runnable example app

---

## Open questions

- **The name.** `socialchimp` is free on PyPI, but a social media tool called
  SocialChimp existed before (2018, now looks closed). Worth a trademark
  search before the first PyPI release. Renaming the GitHub repo is cheap;
  renaming after people install it is not.
