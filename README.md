# sloppy-code

Code I wrote, or mostly wrote, and did **not** seriously review.

The name is not a joke at my own expense for fun. It is a warning label. Nothing in
this repo should be trusted because it is in this repo.

## What "sloppy" means here, concretely

- **Not reviewed.** It may be correct, it may be subtly wrong. I have not read it
  carefully end to end, and neither has anyone else.
- **Not maintained.** No promises, no support, no issue triage. It exists because it
  was useful to me once.
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

## License

None. Do what you like; you were warned.
