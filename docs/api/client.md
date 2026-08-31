# Client

Start here. `SocialChimp` is the class your app creates once. `Account` is
what you get back for a single connection, and it is what you call `.post()`
and `.direct` on.

## SocialChimp

::: socialchimp.client.SocialChimp

## Account

::: socialchimp.client.Account

## Sending your own request

`account.direct` sends a request of your own to the same network, through the
same token, the same retries and the same rate-limit handling. Only the
request itself is yours - see [the tutorial](../tutorial.md#your-first-post)
for why this exists.

::: socialchimp.client.Direct

## Posting to several accounts at once

`SocialChimp.post_to_many` gives every account its own result, so one account
failing never hides the rest.

::: socialchimp.client.PostJob

::: socialchimp.client.PostError

## Keeping tokens working

`SocialChimp` uses this to renew a token a little before it runs out, taking
a lock first so two workers renewing the same connection at once cannot
disconnect an account. You will not normally construct this yourself.

::: socialchimp.tokens.TokenManager
