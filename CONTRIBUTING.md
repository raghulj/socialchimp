# Contributing

Thanks for helping. This page covers how to get set up and the three rules
that are not negotiable.

## Branches

`dev` is where work happens, and it is the default branch, so a fresh clone
puts you there. `main` is what has been released - it is protected, and only
changes through a pull request when a release goes out.

Branch off `dev`, and open your pull request against `dev`.

## Getting set up

```bash
git clone https://github.com/raghulj/socialchimp
cd socialchimp
uv sync --all-extras --dev
uv run pre-commit install --hook-type pre-commit --hook-type pre-push
```

The hooks check formatting, style and types before each commit, and run the
tests before each push. That is deliberate: it means a broken push is hard to
do by accident.

## The three rules

**1. Tests come first.** Write the failing test, watch it fail for the reason
you expect, then write the code. A test written afterwards tends to describe
what the code does rather than what it should do.

**2. Coverage stays at 100%.** `uv run pytest` fails below it. This is not
about the number - it is that a line nobody tested is a line nobody has ever
run, and in a library that talks to nine different networks those lines
pile up fast.

Coverage is a floor, not a finish line. A test that runs a line without
checking anything meaningful passes the gate and helps nobody. Worth trying
on anything tricky: break your own code on purpose and check a test notices.

**3. Types are strict.** `uv run mypy` passes with no `# type: ignore`. If a
type is hard to write, that is usually the design telling you something.

## Language

Write in plain words, in code, docs and error messages alike.

Someone reading this library is usually in the middle of a problem: a token
stopped working, a post did not arrive. They should not also have to decode
our vocabulary. Say "the shared way", not "the abstraction layer". Say
"checking on a timer", not "polling-based virtual webhooks". If a name needs
a glossary, pick a different name.

Error messages should say what went wrong **and what to do about it**.

## Comments

Comments explain *why*, not *what*. The code already says what it does.

The comments worth writing are the ones that stop someone undoing a decision
they do not understand - for example, why token renewal takes a lock and then
reads the connection a second time. Without the why, that reads like a
pointless extra query, and someone will remove it.

## Before you push

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
```

Please do not push red. CI emails are nobody's idea of a good time.

## Adding a network

You do not need to add it here. Publish your own package that registers
itself and socialchimp will find it. See [docs/adding-a-platform.md](docs/adding-a-platform.md).

If you would like it built in, open an issue first so we can agree where it
goes in the order - see [docs/PLAN.md](docs/PLAN.md).

## Licence

By contributing you agree your work is released under the MIT licence.
