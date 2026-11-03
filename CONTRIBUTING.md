# Contributing to pushraker

## Upgrades never break

An upgrade must keep working with what users already have. pushraker runs
on printers that upgrade it from a button, often unattended, so:

- **Settings**: `pushraker.conf` and `pushraker.secrets` from any earlier
  release load as they are. New settings get defaults; a renamed or
  removed setting keeps being read under its old name (see how
  `provider: none` and the old camera names are still understood).
- **Notifications from G-code**: `RESPOND PREFIX=notify MSG="title ||
  message || priority"` keeps its meaning. Today: the title is everything
  before the first ` || `, the priority is the last field when it is one
  of min, low, default, high, max, and the message is everything in
  between. A new field goes after the priority, and `*` in the priority
  place will mean "the default", so `title || message || * || new` stays
  readable by older and newer releases alike.
- **Moonraker endpoints** (`/server/pushraker/*`, and the 0.1 names
  under `/server/push_notifications/*`), the webhook's
  JSON fields and its `event` names, and the Klipper macros
  (`_PRINT_END_NTFY`, `_CANCEL_PRINT_NTFY`, `_NTFY_HOOKS`) only gain
  things; nothing is renamed or removed.
- Upgrades restart Klipper and Moonraker, so a harness never upgrades
  while a print job runs.

If a change can't be made without breaking one of these, it isn't made in
a release on its own: it needs a plan agreed first (a deprecation period
in which both forms work), and harnesses that pin releases (the Snapmaker
U1 extended firmware builds one release in) are told before it ships.

## Commit messages are the release notes

The publish workflow writes each GitHub release's notes from the commits
since the previous release: the subject becomes a bullet, the body goes
under it. Write the subject as what changed for the user, and the body as
the details they'd want; attribution lines (`Co-Authored-By:`) are left
out of the notes.

## Releasing

Create a GitHub release with a `vX.Y.Z` tag (and the same version in
`pyproject.toml` and `src/pushraker/__init__.py`). The workflow builds the
package, publishes it to PyPI and fills in the release notes.

## Tests

`python tests/test_pushraker.py` (about a minute; needs apprise and
Pillow). New behaviour comes with a test.
