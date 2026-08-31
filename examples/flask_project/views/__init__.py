"""The four blueprints this app is made of.

One per job, so each file is short enough to read in one go: the list of
connected accounts, signing somebody in, writing a post, and the address the
networks that push send their updates to.
"""

from __future__ import annotations

from .connections import bp as connections
from .posting import bp as posting
from .signin import bp as signin
from .webhooks import bp as webhooks

__all__ = ["connections", "posting", "signin", "webhooks"]
