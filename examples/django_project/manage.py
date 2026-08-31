#!/usr/bin/env python
"""Django's command-line tool, unchanged except for one line.

Run everything from this directory:

    python manage.py migrate
    python manage.py runserver

Or from the top of the repository, which is what the README does:

    uv run python examples/django_project/manage.py check
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> None:
    """Hand the command line to Django."""
    # This project is not installed as a package, so `socialsite` and `social`
    # are only importable when the directory holding them is on the path.
    # Putting it here rather than relying on the current directory means
    # `python examples/django_project/manage.py check` works from anywhere.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "socialsite.settings")

    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
