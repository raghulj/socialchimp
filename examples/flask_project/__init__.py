"""A small Flask app that connects to all nine networks socialchimp covers.

Clone it, run it, read it. Every file is meant to be read in order:

    config.py       what comes out of the environment, and why Meta needs
                    two pairs of credentials rather than one
    networks.py     the nine networks and what each one needs first
    db.py           four tables, all of them this app's own
    storage.py      the five methods socialchimp calls
    login_notes.py  where a half-finished sign-in waits, and why not in a
                    cookie
    posts.py        turning one form into the post each network will take
    views/          four blueprints: connections, sign-in, posting, webhooks
    factory.py      the application factory that ties them together

Run it with no credentials at all:

    uv run --with flask python -m examples.flask_project.check_it_runs

That builds the app against pretend networks and walks a sign-in of each
shape, a post to several accounts at once with one of them refusing, and a
signed webhook. Nothing leaves the machine.
"""

from __future__ import annotations

from .factory import create_app

__all__ = ["create_app"]
