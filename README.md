# sloppy-code

Code written by an AI coding assistant under my direction, and **not** seriously
reviewed by me or anyone else.

The name is not a joke at my own expense for fun. It is a warning label. Nothing in
this repo should be trusted because it is in this repo.

It's essentially tools built for one purpose. i write the specs, describe what i want,
get it done, do a cursory check for sanity and from time to time review them using
[review](https://github.com/splitbrain/review) (or more precisely, my fork of it,
contributed upstream).

## What "sloppy" means here, concretely

- **Not reviewed.** i directed what it should do; i did not read the result
  carefully end to end, and neither has anyone else. Or maybe i have and made notes,
  but in no way thoroughly.
- **AI-generated.** An assistant wrote the code, including the comments claiming
  they were careful. Treat confident-sounding prose in here as unverified. i tend to
  hit them with a light stick when they do so but you know how they're trained...
- **Not maintained.** No promises, no support, no issue triage. It exists because it
  was useful to me once. i'm open to your contributions though.
- **Possibly wrong in ways that matter.** Some of it touches credentials, file
  permissions, and storage. Read it yourself before you run it.
- **Environment-specific.** It was written against my machines, my paths, my
  versions. Constants that were true for me may be false for you.

## Read this before running anything

- Do not run it against systems you care about without reading it first.
- Do not assume any secret handling is safe just because it looks like it is.
- Do not assume destructive operations are guarded. **Verify.**
- If something here would delete, move, or overwrite data, treat it as hostile until
  you have read the code that does it.

## Layout

Each project gets its own directory. A directory may carry its own README with more
detail — that detail is still unreviewed.

| Directory | What it is |
|---|---|
| `truenas-lldap-password-worker/` | A small HTTP service that lets a user change their password in **two** credential stores at once (an LDAP directory and a TrueNAS host), with a two-stage login gate, IP cooldowns, and operator-supplied HTML banners. |

## Why publish it at all

Because some of it might save someone an afternoon, and because unreviewed code is
less dangerous when it is *labelled* unreviewed. Take the ideas, ignore the
implementation, and check everything.

Open a code review if there's something blatant or just useful.

## Personal notes

i don't use proprietary models, only open-weight. i don't run inference in the cloud.
the technology is good when put in good hands, but most hands tend to be evil. and i
never was a fan of executing stuff on other people's computers (github/gitlab may be
an exception to that rule, because it's a cooperative ecosystem).

this was slopcoded (and written in part) with DeepSeek-V4.1-Flash, as part of a training
and stress tests. Cloud fee: $0.

## License

Artistic license. Fetch it online.
