# Testing helpers

For anyone writing a platform of their own. Installed with
`pip install "socialchimp[testing]"` - see
[Adding a platform](../adding-a-platform.md) for how these prove a platform
behaves like the built-in ones.

## PlatformChecks

A `pytest` mixin: inherit from it, point it at your platform, and it runs
the checks every built-in platform passes.

::: socialchimp.testing.PlatformChecks

## Building a fake platform

::: socialchimp.testing.FakePlatform

## Recording HTTP without a network

::: socialchimp.testing.RecordingTransport

## Recording storage calls

::: socialchimp.testing.RecordingStorage

::: socialchimp.testing.StorageCall
