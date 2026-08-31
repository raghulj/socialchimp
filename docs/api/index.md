# API reference

This is generated straight from the docstrings in `src/socialchimp`, so it
stays honest as the code changes. Every argument, every raise, and why it
works that way, is written once - in the source - and shown here.

Most apps only ever need the first two pages.

| Page | What is in it |
|---|---|
| [Client](client.md) | `SocialChimp`, `Account`, sending your own request, posting to many accounts, keeping tokens working |
| [Data](models.md) | `Post`, `Media`, `Connection`, `Token`, `PostResult`, `PostState` - the objects that pass between your app and socialchimp |
| [Storage](storage.md) | The five methods your app writes, `Storage` and `SyncStorage`, and `InMemoryStorage` for trying things out |
| [Features and limits](features.md) | `Feature`, `Limits`, and the checks that catch a bad post before it costs a request |
| [Updates and events](events.md) | `Update`, `UpdateKind`, verifying a webhook, and polling the networks that cannot push |
| [Errors](errors.md) | Every exception socialchimp raises, and when |
| [Writing a platform](platform.md) | The `Platform` protocol, its `Can...` extras, and `HttpClient` - only needed for a network socialchimp does not support yet |
| [Framework helpers](frameworks.md) | The Django, FastAPI and Flask routes, by signature |
| [Testing helpers](testing.md) | `PlatformChecks` and the fakes used to prove a platform behaves |
