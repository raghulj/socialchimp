# Releasing

## One-time setup: let GitHub publish for you

PyPI can be told to trust one workflow in one repository, so releases go out
with no API token anywhere. Nothing to store, nothing to leak, nothing to
rotate, and nobody can publish from their laptop by accident.

Because `socialchimp` does not exist on PyPI yet, add it as a **pending
publisher** — the project gets created on the first upload.

Go to <https://pypi.org/manage/account/publishing/>, and under *Add a new
pending publisher* choose **GitHub** and fill in exactly:

| Field | Value |
|---|---|
| PyPI project name | `socialchimp` |
| Owner | `raghulj` |
| Repository name | `socialchimp` |
| Workflow name | `publish.yml` |
| Environment name | `pypi` |

Two things people get wrong here:

- **Workflow name is the file name**, `publish.yml` — not `Publish`, which is
  the name inside the file.
- **Environment name must match** the `environment: name: pypi` in
  `.github/workflows/publish.yml`. If you leave it blank on PyPI, it must be
  blank in the workflow too, or the upload is refused.

Optionally, on GitHub under **Settings → Environments → pypi**, add yourself
as a required reviewer. Then every publish waits for you to press a button,
which is a cheap way to make an accidental release impossible.

## Where work happens

`dev` is the default branch and where everything lands. `main` is what has
been released.

`main` is protected: no force pushes, no deletions, CI has to pass on all
three Python versions, and changes arrive by pull request. So a release is a
pull request from `dev` to `main`, and the tag goes on `main`.

Publishing has a second gate on top of that: the `pypi` environment needs
`raghulj` to approve it, so nothing reaches PyPI without somebody pressing a
button, even if a bad commit gets onto `main`.

## Cutting a release

1. **Update the version** in `src/socialchimp/__init__.py`. That is the only
   place it lives; the package metadata reads it from there.

2. **Write the changelog entry** in `CHANGELOG.md`. Say what will surprise
   somebody, not just what changed. Anything that alters behaviour people
   already rely on goes near the top, in plain words.

3. **Run the whole gate**, and do not skip it because CI is green — CI ran on
   the last commit, not on what is in front of you:

   ```bash
   uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
   ```

4. **Check the built package, not just the source.** A wheel can be wrong in
   ways the test suite cannot see:

   ```bash
   rm -rf dist && uv build
   uv venv /tmp/check && VIRTUAL_ENV=/tmp/check uv pip install dist/*.whl
   VIRTUAL_ENV=/tmp/check uv run python -c "
   import socialchimp as sc
   print(sc.__version__, sc.available_platforms())
   "
   ```

   Every network should be listed. If one is missing, its entry point is not
   in `pyproject.toml`, and nobody would be able to load it by name.

5. **Open a pull request from `dev` to `main` and merge it once CI is
   green**, then tag the merge commit:

   ```bash
   gh pr create --base main --head dev --title "Release 0.2.0"
   # once it is merged:
   git checkout main && git pull
   git tag -a v0.2.0 -m "socialchimp 0.2.0"
   git push origin v0.2.0
   git checkout dev
   ```

6. **Publish the GitHub release.** That is what starts the upload:

   ```bash
   gh release create v0.2.0 --title "0.2.0 - what it is" --notes-file notes.md
   ```

   The workflow runs the whole gate again on three Python versions, builds,
   checks the files are uploadable, and only then publishes. **Publishing
   cannot be undone** — PyPI never allows a version number to be reused, even
   after deleting it — so it is worth the few minutes.

## Numbering

While the first number is 0, treat the middle one as the breaking one:
`0.1.x` is safe, `0.2.0` may not be. What that means for people writing their
own networks is in
[adding a platform](adding-a-platform.md#what-we-promise-about-changes).

## Trying it without the real thing

To see the rendered project page before it is permanent, publish to TestPyPI
first. Add a second pending publisher at
<https://test.pypi.org/manage/account/publishing/> with the same values, then:

```bash
uv publish --publish-url https://test.pypi.org/legacy/ --token pypi-...
```
