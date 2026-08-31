# Updates and events

A comment, a like, a post finishing its encoding, somebody removing your
app - every one of these arrives as the same `Update`, whichever network it
came from and whether the network pushed it to you or socialchimp had to
check on a timer. Your code cannot tell the two ways apart, and does not
need to.

## Update

::: socialchimp.events.Update

::: socialchimp.events.UpdateKind

## Receiving pushed updates (webhooks)

::: socialchimp.events.verify_hmac_sha256

::: socialchimp.events.verify_shared_secret

::: socialchimp.events.check_not_too_old

::: socialchimp.events.answer_setup_check

::: socialchimp.events.Dispatcher

## Polling the networks that cannot push

"On a timer" in [Networks](../platforms.md) means this.

::: socialchimp.events.Poller

::: socialchimp.events.poll

::: socialchimp.events.SeenUpdates

::: socialchimp.events.InMemorySeenUpdates
