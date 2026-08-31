"""Ready-made routes for Django, FastAPI and Flask.

Signing someone in and receiving a webhook are the same job on every
framework, and they are the two places it is easiest to get wrong - a state
value that does not survive the round trip, a body that was parsed before its
signature was checked. So they are written once here, and each framework gets
a thin file that only translates between its own request and ours.

    from socialchimp.contrib.fastapi import router
    app.include_router(router(sc, redirect_uri="https://app.example/cb/{platform}"))

Nothing here is the only way in. Every route is a wrapper around a
`SocialChimp` method your app can call itself, so your own URLs, your own
login checks, or a framework we have never heard of are not special cases -
see `socialchimp.contrib.shared`, which is where the work actually happens.

Importing `socialchimp` does not import any of these, and importing one of
them does not import the other two. Install the framework you use:

    pip install "socialchimp[django]"    # or [fastapi], or [flask]

`socialchimp.contrib.shared` needs none of them, and neither does
`sync_storage`, which lets you write your five storage methods as ordinary
blocking code.
"""
