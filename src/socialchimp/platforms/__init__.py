"""The networks that ship with socialchimp.

One module per network. Nothing is imported when socialchimp starts; a
platform's code is loaded the first time somebody asks for it, so having ten
networks installed costs nothing until you use one.

Nothing is exported from here on purpose. Ask for a platform by name instead:

    from socialchimp.registry import get_platform_class

    mastodon = get_platform_class("mastodon")

That way a network someone else published works exactly like one that came in
the box. See `docs/adding-a-platform.md`.
"""
