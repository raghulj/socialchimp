# Data

Everything socialchimp passes around is frozen: once made, it never changes.
A refresh produces a new `Connection` rather than editing the old one, and
anything holding a secret hides it from `repr()`. See
[the tutorial](../tutorial.md#the-four-ideas) for how these fit together.

## Post

::: socialchimp.models.Post

## PostResult and PostState

::: socialchimp.models.PostResult

::: socialchimp.models.PostState

## PostStats

How a published post is doing, in one shape for every network. What
`account.read_stats(...)` hands back. Every number may be `None`, which
means the network does not count that - never that it counted zero.

::: socialchimp.models.PostStats

## Media

::: socialchimp.models.Media

::: socialchimp.models.MediaKind

## Connection

::: socialchimp.models.Connection

## Token

::: socialchimp.models.Token

## App credentials

What `create_app` and a manually-registered app store about themselves.

::: socialchimp.models.AppCredentials

## The social inbox

What reading a post, its thread, its likes and its conversations hands
back - added in 0.8.0. See the
[social inbox use case](../use-cases/social-inbox.md) for working examples.

::: socialchimp.models.Page

::: socialchimp.models.Person

::: socialchimp.models.PostDetails

::: socialchimp.models.Visibility

::: socialchimp.models.TextLink

::: socialchimp.models.LinkKind

::: socialchimp.models.Attachment

::: socialchimp.models.Unavailable

::: socialchimp.models.Thread

::: socialchimp.models.Like

::: socialchimp.models.LikeResult

::: socialchimp.models.Conversation

::: socialchimp.models.Message
