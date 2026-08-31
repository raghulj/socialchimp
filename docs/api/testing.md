# Testing helpers

`PlatformChecks` is for anyone writing a platform of their own. It runs on
pytest, so it wants `pip install "socialchimp[testing]"` - see
[Adding a platform](../adding-a-platform.md) for how it proves a platform
behaves like the built-in ones.

The three doubles below it - `FakePlatform`, `RecordingTransport` and
`RecordingStorage` - need nothing but socialchimp itself. They are for
building an app as much as for testing one, and a program that only uses
those does not want the extra.

## PlatformChecks

A `pytest` mixin: inherit from it, point it at your platform, and it runs
the checks every built-in platform passes. Subclassing it without pytest
installed says so, and says what to install.

::: socialchimp.testing.PlatformChecks

## Building a fake platform

::: socialchimp.testing.FakePlatform

## Recording HTTP without a network

::: socialchimp.testing.RecordingTransport

## Recording storage calls

::: socialchimp.testing.RecordingStorage

::: socialchimp.testing.StorageCall
