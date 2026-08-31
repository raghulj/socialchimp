# Framework helpers

The ready-made routes described in [Frameworks](../frameworks.md), by
signature. Importing `socialchimp` never imports any of these - you only pay
for the one you use.

## Django

::: socialchimp.contrib.django.urls

::: socialchimp.contrib.django.get_client

::: socialchimp.contrib.django.orm_storage

::: socialchimp.contrib.django.Request

::: socialchimp.contrib.django.View

::: socialchimp.contrib.django.MakeResponse

::: socialchimp.contrib.django.MakePath

::: socialchimp.contrib.django.Exempt

::: socialchimp.contrib.django.Settings

## FastAPI

::: socialchimp.contrib.fastapi.router

## Flask

::: socialchimp.contrib.flask.blueprint

::: socialchimp.contrib.flask.run

## Shared by all three

Nothing here knows what a request object looks like or imports any
framework. Each framework's file takes a request apart into plain values,
calls something here, and turns the result back into that framework's own
response.

::: socialchimp.contrib.shared.Routes

::: socialchimp.contrib.shared.Reply

::: socialchimp.contrib.shared.status_for

::: socialchimp.contrib.shared.read_form

::: socialchimp.contrib.shared.LoginMemory

::: socialchimp.contrib.shared.InMemoryLoginMemory
