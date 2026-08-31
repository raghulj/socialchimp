# Errors

Every error socialchimp raises is a `SocialChimpError`. Catch that one if you
only want to know something went wrong; catch a specific subclass if your
app needs to react differently - retry, ask the person to sign in again, or
show them what the network actually said.

`ConfigError` and `InvalidPostError` are a `ValueError` as well. Both are
raised for a value your code handed us, and both were a bare `ValueError`
before 0.3.0, so code written against that still works. Catch
`SocialChimpError`; you do not need to catch both.

::: socialchimp.errors.SocialChimpError

## Setting things up

::: socialchimp.errors.ConfigError

## Signing in and tokens

::: socialchimp.errors.AuthError

::: socialchimp.errors.TokenExpiredError

## What the network would not do

::: socialchimp.errors.NotAllowedError

::: socialchimp.errors.NotFoundError

::: socialchimp.errors.RateLimitError

::: socialchimp.errors.InvalidPostError

::: socialchimp.errors.NotSupportedError

## Talking to the network

::: socialchimp.errors.NetworkError

::: socialchimp.errors.PlatformError

## Webhooks

::: socialchimp.errors.SignatureError
