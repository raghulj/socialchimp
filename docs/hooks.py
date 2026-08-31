"""Build-time work this site needs that mkdocs cannot do on its own.

Two unrelated jobs live here, because mkdocs only looks at one file for
hooks (see `hooks:` in mkdocs.yml).

**Link fixes**, for content this site does not own the wording of. A few
pages link to things that live outside `docs_dir` - the source tree, the
runnable examples, and two files kept at the repository root
(`CONTRIBUTING.md`, `CHANGELOG.md`) so that GitHub renders them without a
detour through the docs site. MkDocs only knows about files under `docs/`,
so a plain relative link like `../examples/post_to_each.py` resolves to
nothing once the site is built. Rather than editing the wording of pages we
do not own, `on_page_markdown` rewrites those specific links at build time:

- A link that walks out of `docs/` to reach `src/` or `examples/` becomes a
  link to that path on GitHub.
- `contributing.md` and `changelog.md` are not separate prose - they are the
  root `CONTRIBUTING.md` and `CHANGELOG.md`, read straight off disk here, so
  there is exactly one copy of the words. Those two files are written to be
  read from the repository root (links like `docs/adding-a-platform.md`), so
  the `docs/` prefix is stripped to match where this hook places them.

**Machine-readable output**, for a coding assistant pointed at this site
instead of a person reading it in a browser:

- `/networks.json` - what every network can do, read from the platform
  classes themselves rather than written by hand, so it cannot say something
  the code does not back up. `docs/networks.md` is the same facts as a page
  a person can read; `_networks_matrix` builds the data once and both are
  generated from it.
- `/llms.txt` - a short curated index, in the shape llmstxt.org describes:
  an H1, a one-line summary, and link lists under H2 headings.
- `/llms-full.txt` - every page on this site, concatenated, for a model that
  would rather fetch once than follow links.

All three name the exact version of socialchimp they describe, read from
`socialchimp.__version__`, because the whole point of them is answering for
a specific version rather than "whatever this library happens to be doing
now".

See mkdocs.yml (`hooks:`) for how this is wired in.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import re
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from socialchimp import __version__
from socialchimp.features import Feature, TextCount
from socialchimp.models import Connection, Token
from socialchimp.platform import (
    CanAnswerSetupCheck,
    CanCheckSignature,
    CanCheckState,
    CanCreateApp,
    CanDeletePosts,
    CanReadUpdates,
    CanResumeLogin,
)
from socialchimp.registry import available_platforms, get_platform_class

if TYPE_CHECKING:
    from collections.abc import Callable

    from mkdocs.config.defaults import MkDocsConfig
    from mkdocs.structure.files import Files
    from mkdocs.structure.pages import Page

    from socialchimp.platform import Platform

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


# ---------------------------------------------------------------------------
# The networks capability matrix.
#
# Every fact below is read from a platform class rather than typed out by
# hand: `Feature` flags and `POST_OPTIONS` come straight off the class and
# its module, which extras it satisfies is answered the same way
# `SocialChimp` itself decides it - see `_extras` - and a limit is only
# reported when the method that returns it needs nothing from the network to
# answer, which `_is_static` works out by reading the method's own code
# rather than being told which networks to trust.
# ---------------------------------------------------------------------------

# A connection good enough to hand to a `limits()` that never looks at it -
# every platform whose numbers are fixed says so in its own docstring ("Not
# used here"). Never sent anywhere: nothing that reaches this is real.
_SAMPLE_CONNECTION: Final = Connection(
    id="docs-sample",
    platform="docs-sample",
    host=None,
    account_id="docs-sample",
    account_name="docs sample account",
    token=Token(access_token="docs-sample-value"),  # noqa: S106
)


def _is_static(method: Callable[..., Any]) -> bool:
    """Say whether an async method's own body ever awaits anything.

    Used to tell a `limits()` that is arithmetic on numbers written in this
    file (Bluesky, Facebook, Pinterest, TikTok, X, YouTube) from one that
    has to ask a server or the network for at least part of its answer
    (Mastodon asks its server what it allows; Instagram and Threads ask how
    many posts are left today). Only the method's own body is looked at, not
    whatever it goes on to call, which is exactly the question this file
    needs answered: can this be read off the class alone, with no account
    and no network, or not.

    Args:
        method: The method to inspect, for example `MastodonPlatform.limits`.

    Returns:
        True if nothing in the method's own body is awaited.
    """
    source = textwrap.dedent(inspect.getsource(method))
    tree = ast.parse(source)
    return not any(isinstance(node, ast.Await) for node in ast.walk(tree))


def _static_limits(platform: Platform) -> dict[str, Any] | None:
    """Return what `platform.limits()` says, or None where that needs a live account.

    Args:
        platform: An instance built with no arguments.

    Returns:
        `Limits` as plain data, with the enum turned into the string
        `TextCount` already stores it as, or `None` when the real number can
        only come from asking a server or the network.
    """
    if not _is_static(type(platform).limits):
        return None
    limits = asyncio.run(platform.limits(_SAMPLE_CONNECTION))
    return {
        "max_text_length": limits.max_text_length,
        "max_text_bytes": limits.max_text_bytes,
        "text_counted_in": limits.text_counted_in.value,
        "max_images": limits.max_images,
        "max_image_bytes": limits.max_image_bytes,
        "max_title_length": limits.max_title_length,
        "max_videos": limits.max_videos,
        "max_video_bytes": limits.max_video_bytes,
    }


def _sign_in_shapes(cls: type[Platform]) -> list[str]:
    """List the `LoginStep` shapes this platform's sign-in actually returns.

    Read from the return annotation each method declares - `start_login`
    always, then `finish_login` and `resume_login` where it has one - in the
    order a sign-in calls them. A platform with no `resume_login` never
    pauses to ask which account to use.

    Args:
        cls: The platform class.

    Returns:
        Shape names in call order, each named once even if it is returned by
        more than one step.
    """
    shapes: list[str] = []
    for method_name in ("start_login", "finish_login", "resume_login"):
        method = getattr(cls, method_name, None)
        if method is None:
            continue
        shape = inspect.signature(method).return_annotation
        if isinstance(shape, str) and shape not in shapes:
            shapes.append(shape)
    return shapes


def _post_options(cls: type[Platform]) -> tuple[str, ...]:
    """Read the `POST_OPTIONS` a platform's own module declares.

    Args:
        cls: The platform class.

    Returns:
        The option names, in the order the module lists them, or an empty
        tuple for a module that names none.
    """
    module = inspect.getmodule(cls)
    options: tuple[str, ...] = getattr(module, "POST_OPTIONS", ())
    return tuple(options)


def _alt_text_reaches(cls: type[Platform]) -> bool:
    """Say whether `Media.alt_text` reaches this network at all.

    Some networks read it straight off the attachment; Pinterest instead
    takes it as a post option, because Pinterest hangs a description off the
    whole pin rather than off one picture. Both show up as the string
    `"alt_text"` somewhere in the module, which is what this checks rather
    than hand-listing which networks take it - a network that gains it later
    is picked up here without this file changing.

    Args:
        cls: The platform class.

    Returns:
        True if the platform's module mentions `alt_text` anywhere.
    """
    module = inspect.getmodule(cls)
    source = inspect.getsource(module) if module is not None else ""
    return "alt_text" in source


def _extras(instance: Platform, features: Feature) -> list[str]:
    """List which optional extras this platform actually satisfies.

    Mirrors exactly what `SocialChimp` itself checks before calling one of
    these - two of them (`create_app`, `delete_post`) are gated on a
    `Feature` flag as well as the method existing, because a network that
    cannot do a thing may still carry a method that only exists to explain
    that in a clear error rather than an `AttributeError`. Facebook's
    `create_app` is exactly this: present, and always refuses, because Meta
    has no way to register an app for you. Checking the method alone would
    have called that "yes".

    Args:
        instance: A platform built with no arguments.
        features: `instance.features`.

    Returns:
        The names of the extras from `socialchimp.platform` this platform
        satisfies, in a fixed order shared with `/networks.json`'s own
        listing of the seven extras.
    """
    satisfied: list[str] = []
    if Feature.CREATE_APP in features and isinstance(instance, CanCreateApp):
        satisfied.append("CanCreateApp")
    if isinstance(instance, CanResumeLogin):
        satisfied.append("CanResumeLogin")
    if isinstance(instance, CanCheckState):
        satisfied.append("CanCheckState")
    if isinstance(instance, CanCheckSignature):
        satisfied.append("CanCheckSignature")
    if isinstance(instance, CanReadUpdates):
        satisfied.append("CanReadUpdates")
    if isinstance(instance, CanAnswerSetupCheck):
        satisfied.append("CanAnswerSetupCheck")
    if Feature.DELETE_POST in features and isinstance(instance, CanDeletePosts):
        satisfied.append("CanDeletePosts")
    return satisfied


def _network_facts(name: str) -> dict[str, Any]:
    """Build one network's entry in the capability matrix.

    Args:
        name: The name it is registered under, for example `"mastodon"`.

    Returns:
        Everything `/networks.json` and `docs/networks.md` say about it.
    """
    cls = get_platform_class(name)
    instance = cls()
    features = instance.features
    return {
        "name": name,
        "features": [flag.name for flag in Feature if flag in features and flag.name],
        "post_options": list(_post_options(cls)),
        "extras": _extras(instance, features),
        "limits": _static_limits(instance),
        "sign_in_shapes": _sign_in_shapes(cls),
        "alt_text_reaches_it": _alt_text_reaches(cls),
    }


def _networks_matrix() -> dict[str, Any]:
    """Build the whole capability matrix, one entry per installed platform.

    Returns:
        The data behind both `/networks.json` and `docs/networks.md`.
    """
    return {
        "socialchimp_version": __version__,
        "source": (
            "Generated at build time from the installed socialchimp "
            "package (docs/hooks.py). Never hand-written, so it cannot "
            "drift from the code."
        ),
        "networks": {name: _network_facts(name) for name in available_platforms()},
    }


def _limits_sentence(limits: dict[str, Any] | None) -> str:
    """Turn one network's limits into a sentence for `docs/networks.md`.

    Args:
        limits: What `_static_limits` returned for this network.

    Returns:
        A plain sentence, or one saying the numbers are checked live.
    """
    if limits is None:
        return (
            "Checked live against the account or server, not listed here - "
            "see the network's own page in [Networks](platforms.md)."
        )

    counted_in = TextCount(limits["text_counted_in"])
    parts = []
    if limits["max_text_length"] is not None:
        parts.append(f"up to {limits['max_text_length']} {counted_in.in_words}")
    if limits["max_text_bytes"] is not None:
        parts.append(f"and {limits['max_text_bytes']} bytes")
    if limits["max_title_length"] is not None:
        parts.append(f"a title up to {limits['max_title_length']} characters")
    if limits["max_images"] is not None:
        parts.append(f"up to {limits['max_images']} pictures")
    if limits["max_image_bytes"] is not None:
        parts.append(f"each up to {limits['max_image_bytes']} bytes")
    if limits["max_videos"] is not None:
        parts.append(f"up to {limits['max_videos']} video(s)")
    if limits["max_video_bytes"] is not None:
        parts.append(f"up to {limits['max_video_bytes']} bytes each")
    return (", ".join(parts) + ".") if parts else "Nothing declared."


def _networks_page_markdown(config: MkDocsConfig, matrix: dict[str, Any]) -> str:
    """Write `docs/networks.md` from the same data `/networks.json` holds.

    Args:
        config: The site configuration, for the full address of
            `/networks.json` - written out in full rather than as a
            relative link, because `networks.json` is not a page mkdocs
            knows about and a relative link to it would not resolve.
        matrix: `_networks_matrix()`.

    Returns:
        The whole page, as markdown.
    """
    version = matrix["socialchimp_version"]
    json_url = _absolute_url(config, "networks.json")
    lines = [
        "# Networks capability matrix",
        "",
        f"Generated from socialchimp {version}'s own platform classes at "
        "build time - not hand-written, so it cannot say something the code "
        f"does not back up. The same data as JSON is at [`networks.json`]"
        f"({json_url}), for a coding assistant to read in one fetch.",
        "",
        "For each network: the `Feature` flags it declares, the option "
        "names its `POST_OPTIONS` accepts, which of the optional extras in "
        "[`socialchimp.platform`](api/platform.md) it satisfies, its sign-in "
        "shape in call order, whether `Media.alt_text` reaches it, and its "
        "posting limits where those are fixed rather than looked up live.",
        "",
    ]
    for name, facts in matrix["networks"].items():
        lines.extend(
            [
                f"## {name}",
                "",
                f"- **Features:** {', '.join(facts['features'])}",
                f"- **Post options:** {', '.join(facts['post_options']) or 'none'}",
                f"- **Extras:** {', '.join(facts['extras']) or 'none'}",
                f"- **Sign-in shape:** {' → '.join(facts['sign_in_shapes'])}",
                "- **Alt text reaches it:** "
                + ("yes" if facts["alt_text_reaches_it"] else "no"),
                f"- **Limits:** {_limits_sentence(facts['limits'])}",
                "",
            ]
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# llms.txt and llms-full.txt.
#
# `_files` and `_pages` are filled in as one build runs: `on_files` clears
# them for the build that is starting, `on_page_markdown` adds each page as
# it is rendered, and `on_post_build` reads both once every page is done.
# ---------------------------------------------------------------------------

_files: Files | None = None
_pages: list[tuple[Page, str]] = []

# Every link `llms.txt` lists, grouped under the heading it appears under
# and paired with one sentence about it. Deliberately smaller than the full
# nav - `docs/PLAN.md` is left out of this curated list on purpose, the way
# an internal roadmap would be, but it is still in `llms-full.txt` because
# that file is the whole site, not a curated view of it.
_CURATED_SECTIONS: Final[tuple[tuple[str, tuple[tuple[str, str], ...]], ...]] = (
    (
        "Start here",
        (
            (
                "index.md",
                "What socialchimp is, in a few paragraphs, with a first example.",
            ),
            (
                "assistant.md",
                "The mental model in one dense page: connections, storage, "
                "the four sign-in shapes, why socialchimp raises, and what "
                "changed in 0.3.0. Read this first.",
            ),
            (
                "getting-started.md",
                "Install it, register a Mastodon app, sign someone in, and "
                "publish a first post - then the same code against Bluesky.",
            ),
            (
                "tutorial.md",
                "A longer walkthrough: signing accounts in and posting on "
                "their behalf.",
            ),
        ),
    ),
    (
        "Networks",
        (
            (
                "platforms.md",
                "What each of the nine networks can and cannot do, in "
                "prose, with the surprises named.",
            ),
            (
                "networks.md",
                "The same facts as a table, generated from the platform "
                "classes: features, post options, limits, sign-in shape, "
                "alt text.",
            ),
        ),
    ),
    (
        "Frameworks and examples",
        (
            (
                "frameworks.md",
                "The Django, FastAPI and Flask helpers: routes for sign-in "
                "and webhooks, ready to mount.",
            ),
            (
                "use-cases/facebook-django.md",
                "A worked example: a shop posting product photos to a "
                "Facebook Page from Django.",
            ),
            (
                "use-cases/tiktok-fastapi.md",
                "A worked example: uploading video to TikTok from FastAPI.",
            ),
            (
                "use-cases/youtube-shorts-flask.md",
                "A worked example: publishing YouTube Shorts from Flask.",
            ),
        ),
    ),
    (
        "API reference",
        (
            ("api/index.md", "How the API reference is organised."),
            (
                "api/client.md",
                "`SocialChimp` and `Account` - the two classes an app calls.",
            ),
            (
                "api/models.md",
                "`Post`, `Media`, `Connection`, `Token` - the data every "
                "platform shares.",
            ),
            (
                "api/platform.md",
                "What a platform class provides, for writing your own.",
            ),
            (
                "api/storage.md",
                "The five methods your own storage class implements.",
            ),
            (
                "api/features.md",
                "`Feature`, `Limits`, and the checks that run on a post "
                "before it is sent.",
            ),
            (
                "api/events.md",
                "`Update`, `Dispatcher`, `Poller` - one shape for "
                "everything a network tells you happened.",
            ),
            (
                "api/errors.md",
                "Every error socialchimp raises, and what causes each one.",
            ),
            ("api/frameworks.md", "The framework helpers' full reference."),
            (
                "api/testing.md",
                "`FakePlatform` and the checks for testing a platform of your own.",
            ),
        ),
    ),
    (
        "Contributing",
        (
            (
                "adding-a-platform.md",
                "Writing a platform for a network socialchimp does not cover yet.",
            ),
            ("contributing.md", "How to propose a change."),
            ("releasing.md", "How a release is cut."),
            (
                "changelog.md",
                "Every change, newest first. Read the 0.3.0 section before "
                "writing code against this version - post_to_many was "
                "removed there.",
            ),
        ),
    ),
)


def _absolute_url(config: MkDocsConfig, path: str) -> str:
    """Turn a path relative to the built site into a full address.

    `llms.txt` and its companions are meant to be read outside a browser,
    often in one fetch with nothing else open to resolve a relative link
    against, so every link in them is written out in full.

    Args:
        config: The site configuration, for `site_url`.
        path: Where the thing is, relative to the site root - a page's own
            `File.url` (the home page's is the literal string `"./"`,
            mkdocs's own way of saying "the site root"), or a plain
            filename for something this hook writes straight into
            `site_dir`.

    Returns:
        A complete `https://` address.
    """
    site_url = config.site_url or "/"
    if path in {".", "./"}:
        path = ""
    return f"{site_url.rstrip('/')}/{path.lstrip('/')}"


def _file_url(path: str) -> str:
    """Look up where one curated page ends up in the built site.

    Args:
        path: Its path under `docs_dir`, for example `"getting-started.md"`.

    Returns:
        Its `File.url`.

    Raises:
        LookupError: If nothing in the build has that path. A page named
            here has been renamed or removed without `_CURATED_SECTIONS`
            being updated to match - worth failing loudly on rather than
            shipping a dead link in `llms.txt`.
    """
    found = _files.get_file_from_path(path) if _files is not None else None
    if found is None:
        message = (
            f"docs/hooks.py wants to link to {path!r} in llms.txt, but no "
            f"such page exists in this build. Update _CURATED_SECTIONS."
        )
        raise LookupError(message)
    return found.url


def _llms_txt(config: MkDocsConfig) -> str:
    """Build the curated index at `/llms.txt`.

    Follows the shape [llmstxt.org](https://llmstxt.org) describes: an H1
    naming the project, a blockquote summary, optional prose with no
    headings of its own, then H2-delimited link lists.

    Args:
        config: The site configuration.

    Returns:
        The whole file.
    """
    site_url = (config.site_url or "/").rstrip("/") + "/"
    lines = [
        "# socialchimp",
        "",
        f"> {config.site_description}",
        "",
        f"This file describes socialchimp {__version__}. socialchimp is "
        "pre-1.0, and a change to the middle version number can break "
        "something - match this file to the version you actually have "
        "installed rather than assuming it is current. The same "
        f"documentation as one plain-text file is at {site_url}llms-full.txt, "
        "and the exact features, post options, sign-in shape and limits for "
        f"every network are at {site_url}networks.json, read from the "
        "platform classes rather than written by hand.",
        "",
    ]

    for heading, pages in _CURATED_SECTIONS:
        lines.append(f"## {heading}")
        lines.append("")
        for path, blurb in pages:
            url = _absolute_url(config, _file_url(path))
            lines.append(f"- [{path}]({url}): {blurb}")
        lines.append("")

    lines.append("## Machine-readable")
    lines.append("")
    lines.append(
        f"- [networks.json]({site_url}networks.json): every network's "
        "features, post options, extras, static limits, sign-in shape and "
        "alt-text support, as JSON."
    )
    lines.append(
        f"- [llms-full.txt]({site_url}llms-full.txt): this entire "
        "documentation site as one plain-text file."
    )
    lines.append("")
    return "\n".join(lines)


def _page_title(markdown: str, fallback: str) -> str:
    """Find a page's title from its own first heading.

    Args:
        markdown: The page's markdown, after this hook's own link rewrites.
        fallback: What to use if the page has no top-level heading.

    Returns:
        The heading text, or `fallback`.
    """
    found = re.search(r"^#\s+(.+?)\s*$", markdown, re.MULTILINE)
    return found.group(1) if found else fallback


def _llms_full_txt(config: MkDocsConfig, pages: list[tuple[Page, str]]) -> str:
    """Build the whole site as one file, for a single-fetch read.

    Args:
        config: The site configuration.
        pages: Every page rendered this build, in the order mkdocs rendered
            them, paired with its final markdown (after this hook's own
            link rewrites, so the links read the same as on the site).

    Returns:
        The whole file.
    """
    lines = [
        f"# socialchimp {__version__} - full documentation",
        "",
        "Every page on this site, one after another, for a model that would "
        "rather fetch once than follow links. See "
        f"{_absolute_url(config, 'llms.txt')} for a short curated index "
        f"instead, and {_absolute_url(config, 'networks.json')} for the "
        "capability matrix in `docs/networks.md` below as machine-readable "
        "data.",
        "",
        "=" * 80,
    ]
    for page, markdown in pages:
        title = _page_title(markdown, page.file.src_uri)
        url = _absolute_url(config, page.file.url)
        lines.extend(
            ["", f"## {title}", "", f"({url})", "", markdown.strip(), "", "-" * 80]
        )
    lines.append("")
    return "\n".join(lines)


def on_files(files: Files, *, config: MkDocsConfig) -> Files:
    """Remember this build's files, and start a fresh page collection.

    `on_post_build` needs to know where a curated page ends up (`_file_url`)
    and needs every page's rendered markdown (`_llms_full_txt`), and neither
    is available to it directly - so both are gathered here and in
    `on_page_markdown` as the build runs.

    Args:
        files: Every file this build knows about.
        config: The site configuration. Unused; required by the hook's
            signature.

    Returns:
        `files`, unchanged.
    """
    global _files, _pages
    _files = files
    _pages = []
    return files


def on_page_markdown(
    markdown: str,
    *,
    page: Page,
    config: MkDocsConfig,
    files: Files,
) -> str:
    """Rewrite links that would otherwise point outside the built site.

    Also stands in for the mirrored root files (`contributing.md`,
    `changelog.md`), generates `docs/networks.md` from the same data as
    `/networks.json`, fills in `{{SOCIALCHIMP_VERSION}}` wherever a page
    writes it, and remembers every page's final markdown for
    `/llms-full.txt`.

    Args:
        markdown: The page's own markdown, as mkdocs read it from disk.
        page: The page being rendered.
        config: The site configuration.
        files: Every file in the build. Unused; required by the hook's
            signature.

    Returns:
        The markdown to render, with this site's own link fixes applied.
    """
    mirrors_root_file = _MIRRORS.get(page.file.src_uri)
    if mirrors_root_file is not None:
        markdown = (_ROOT / mirrors_root_file).read_text(encoding="utf-8")
        # Written to be read from the repository root; this page lives one
        # level down, at the same depth as its siblings in docs/.
        markdown = markdown.replace("](docs/", "](")

    if page.file.src_uri == "networks.md":
        markdown = _networks_page_markdown(config, _networks_matrix())

    markdown = markdown.replace("{{SOCIALCHIMP_VERSION}}", __version__)
    markdown = _OUTSIDE_DOCS.sub(_repo_link, markdown)
    markdown = markdown.replace("](../CONTRIBUTING.md)", "](contributing.md)")

    _pages.append((page, markdown))
    return markdown


def on_post_build(*, config: MkDocsConfig) -> None:
    """Write the files a coding assistant reads instead of the rendered site.

    Runs once the whole site is built, so every page mkdocs knows about
    (`_files`) and every page's final markdown (`_pages`) are both ready.
    Writes straight into `site_dir` - these are not pages with nav entries
    of their own, so mkdocs has no other way to produce them.

    Args:
        config: The site configuration, for `site_dir` and `site_url`.
    """
    site_dir = Path(config.site_dir)
    matrix = _networks_matrix()

    (site_dir / "networks.json").write_text(
        json.dumps(matrix, indent=2) + "\n", encoding="utf-8"
    )
    (site_dir / "llms.txt").write_text(_llms_txt(config), encoding="utf-8")
    (site_dir / "llms-full.txt").write_text(
        _llms_full_txt(config, _pages), encoding="utf-8"
    )
