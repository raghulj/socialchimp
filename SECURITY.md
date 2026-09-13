# Reporting a security problem

**Please do not open a public issue.**

Use GitHub's private reporting:
[report a vulnerability](https://github.com/raghulj/socialchimp/security/advisories/new).
It goes only to the maintainers, and we can prepare a fix and release it
before anything is public.

## What is worth reporting

This library holds people's access tokens and checks that incoming requests
really came from a social network, so the things that matter most are:

- **A token leaking** - into a log line, an error message, a traceback, or a
  `repr()`. Everything holding a secret is meant to hide it from `repr()`;
  somewhere that does not is a real problem.
- **A signature check that can be fooled** - a webhook accepted that should
  have been refused, a timing attack on a comparison, a replay of an old but
  validly signed request.
- **A token renewed or stored wrongly** in a way that could let one
  connection act as another.
- **Anything that makes socialchimp send a token somewhere it should not go.**

## What is not a vulnerability

- A network refusing your app because it has not been through that network's
  review. That is the network, and it is covered in
  [what each network needs](https://raghulj.github.io/socialchimp/platforms/).
- Anything requiring an attacker who already has your database or your
  application's own credentials.

## What happens next

We will acknowledge the report, tell you whether we agree it is a problem,
and keep you posted while it is fixed. If you would like credit in the
release notes, say so; if you would rather not be named, that is fine too.

## Versions

socialchimp is pre-1.0. Fixes go into the current release only - there are
no maintained older branches. Upgrading is the fix.
