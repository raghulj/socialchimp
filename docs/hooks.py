"""Build-time link fixes for content this site does not own the wording of.

A few pages link to things that live outside `docs_dir` - the source tree,
the runnable examples, and two files kept at the repository root
(`CONTRIBUTING.md`, `CHANGELOG.md`) so that GitHub renders them without a
detour through the docs site. MkDocs only knows about files under `docs/`,
so a plain relative link like `../examples/post_to_many.py` resolves to
nothing once the site is built.

Rather than editing the wording of pages we do not own, this hook rewrites
those specific links at build time:

- A link that walks out of `docs/` to reach `src/` or `examples/` becomes a
  link to that path on GitHub.
- `contributing.md` and `changelog.md` are not separate prose - they are the
  root `CONTRIBUTING.md` and `CHANGELOG.md`, read straight off disk here, so
  there is exactly one copy of the words. Those two files are written to be
  read from the repository root (links like `docs/adding-a-platform.md`), so
  the `docs/` prefix is stripped to match where this hook places them.

See mkdocs.yml (`hooks:`) for how this is wired in, and the launch report
for the exact lines this covers.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mkdocs.config.defaults import MkDocsConfig
    from mkdocs.structure.files import Files
    from mkdocs.structure.pages import Page

_REPO = "https://github.com/raghulj/socialchimp"
_BRANCH = "dev"
_ROOT = Path(__file__).resolve().parent.parent

# A markdown link that climbs out of docs/ (one or more "../") to reach the
# source tree or the examples - e.g. "../src/socialchimp/platforms/x.py" or
# "../../examples/tiktok_fastapi/tiktok_app.py". Group 1 is the repo-root
# relative path; group 2 is present only when the link points at a
# directory (it ends in "/"), which needs "tree" instead of "blob" on GitHub.
_OUTSIDE_DOCS = re.compile(r"\]\((?:\.\./)+((?:src|examples)/[^)\s#]*?)(/?)\)")

# Files that are the root file of the same name, included verbatim so the
# words live in exactly one place.
_MIRRORS = {
    "contributing.md": "CONTRIBUTING.md",
    "changelog.md": "CHANGELOG.md",
}


def _repo_link(match: re.Match[str]) -> str:
    path, trailing_slash = match.group(1), match.group(2)
    kind = "tree" if trailing_slash else "blob"
    return f"]({_REPO}/{kind}/{_BRANCH}/{path}{trailing_slash})"


def on_page_markdown(
    markdown: str,
    *,
    page: Page,
    config: MkDocsConfig,
    files: Files,
) -> str:
    """Rewrite links that would otherwise point outside the built site."""
    mirrors_root_file = _MIRRORS.get(page.file.src_uri)
    if mirrors_root_file is not None:
        markdown = (_ROOT / mirrors_root_file).read_text(encoding="utf-8")
        # Written to be read from the repository root; this page lives one
        # level down, at the same depth as its siblings in docs/.
        markdown = markdown.replace("](docs/", "](")

    markdown = _OUTSIDE_DOCS.sub(_repo_link, markdown)
    markdown = markdown.replace("](../CONTRIBUTING.md)", "](contributing.md)")
    return markdown
