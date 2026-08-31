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

## Posting to more than one account

There is nothing here for it, on purpose. `Account.post` posts as one account
and raises if that account fails; looping over your accounts, and deciding
what one failure means for the rest, is your app's job. See
[the tutorial](../tutorial.md#posting-to-several-accounts-is-your-loop).

## Keeping tokens working

`SocialChimp` uses this to renew a token a little before it runs out, taking
a lock first so two workers renewing the same connection at once cannot
disconnect an account. You will not normally construct this yourself.

::: socialchimp.tokens.TokenManager
