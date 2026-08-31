# Features and limits

What a network can do is fixed, and stated once by its platform file. What a
particular post is allowed to contain changes while the app runs - a
Mastodon server's post length, an Instagram account's posts left today - so
it is looked up instead. See [Networks](../platforms.md) for what each one
actually supports.

## Feature

::: socialchimp.features.Feature

## Limits

::: socialchimp.features.Limits

## How text is counted

Hardly any network means "characters" when it says "300". `TextCount` says
which counting a network actually uses, and `measure_text` counts a string
the same way.

::: socialchimp.features.TextCount

::: socialchimp.features.measure_text

::: socialchimp.features.count_graphemes

## Checking a post before it is sent

The two checks every platform runs before spending a request. Both refuse in
plain words rather than letting the network answer with a code.

::: socialchimp.features.check_post

::: socialchimp.features.check_option_names
