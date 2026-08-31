# Writing a platform

You only need this page if you are adding support for a network socialchimp
does not have yet - see [Adding a platform](../adding-a-platform.md) for the
story. Everyone else can skip straight to [Networks](../platforms.md).

## The protocol

Every platform provides these seven methods. A platform never subclasses
anything; matching the shape is enough.

::: socialchimp.platform.Platform

## What a platform can opt into

Anything a network cannot do is left off rather than stubbed - a platform
with no `create_app` simply has no `create_app` method, and socialchimp asks
before calling it.

::: socialchimp.platform.CanCreateApp

::: socialchimp.platform.CanResumeLogin

::: socialchimp.platform.CanDeletePosts

::: socialchimp.platform.CanReadUpdates

::: socialchimp.platform.CanCheckSignature

::: socialchimp.platform.CanCheckState

::: socialchimp.platform.CanAnswerSetupCheck

::: socialchimp.platform.CanReadPushedUpdates

## Signing someone in

`start_login` and `finish_login` return one of these four. socialchimp's
type for "one of these four" is `LoginStep = SendToNetwork | AskForDetails |
ChooseAccount | Finished`.

::: socialchimp.platform.LoginRequest

::: socialchimp.platform.SendToNetwork

::: socialchimp.platform.LoginField

::: socialchimp.platform.AskForDetails

::: socialchimp.platform.AccountChoice

::: socialchimp.platform.ChooseAccount

::: socialchimp.platform.Finished

## Finding installed platforms

How `SocialChimp` turns a name like `"facebook"` into a platform instance.
See `socialchimp.registry` for the full story of how packages register
themselves.

::: socialchimp.registry.register_platform

::: socialchimp.registry.unregister_platform

::: socialchimp.registry.available_platforms

::: socialchimp.registry.get_platform_class

::: socialchimp.registry.clear_platform_cache

## Making requests

Every platform file sends its requests through `HttpClient`: retrying after
a hiccup, waiting as long as a network asks, and turning an unhappy reply
into a socialchimp error, written once instead of nine times.

::: socialchimp.http.HttpClient

::: socialchimp.http.Retries

::: socialchimp.http.RateLimit

::: socialchimp.http.rate_limit_from_headers

::: socialchimp.http.retry_after_seconds

::: socialchimp.http.error_from_response

::: socialchimp.http.read_body
