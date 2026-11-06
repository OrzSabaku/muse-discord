# Releasing muse-discord

For maintainers. A release is a semver tag on `main` plus a GitHub
Release whose notes come from CHANGELOG.md. Every install offers its
owners each release.

## When to release

When `main` has user-facing change worth an owner's attention: a
feature, or a batch of fixes. A bad bug's fix can go out alone as a
patch. Docs-only and CI-only changes need no release.

## Choosing the version

Tags are `vMAJOR.MINOR.PATCH`:

- **MAJOR:** updating needs something risky from the owner, or an
  existing `config.yaml`, plugin or worker prompt stops working.
- **MINOR:** new features; old configs keep working.
- **PATCH:** fixes only.

Pre-releases such as `v1.2.0-rc.1` are published as GitHub
pre-releases, which installs ignore. They use their own CHANGELOG entry
if there is one, else the upcoming `v1.2.0` entry.

## Writing the CHANGELOG entry

Every user-visible change gets a line in the entry at the top of
CHANGELOG.md:

```markdown
## v1.2.0 (2026-11-02): Short title an owner understands

Added:

- ...

**Action needed:**

- What an existing install must do after updating.
```

The update DM shows owners each release's title and whether it has an
**Action needed** list, so write both for owners. Leave the list out
when there is nothing to do; UPDATING.md walks a Muse through it.

Keep existing installs working:

- Every new setting gets a built-in default in `muse_settings.py`, and
  every renamed one keeps mapping from its old name.
- Never edit `persona.md`: it is the owner's file. The default persona
  is `DEFAULT_PERSONA` in `bin/connect-discord.py`; setup replaces only
  the shipped placeholder, which `SHIPPED_PERSONAS` recognizes by hash.

## Cutting the release

1. Open a PR that adds the entry (dated for the release) and merge it.
2. On an up-to-date `main`, run `bin/release.sh 1.2.0`. It checks the
   tree, the CHANGELOG entry and that the tag is new, shows what it
   will do, asks to confirm (`--yes` skips that), then pushes an
   annotated tag `v1.2.0`.

The push runs `.github/workflows/release.yml`, which publishes the
Release titled `v1.2.0: Short title` with the entry as notes, or fails
naming the heading it expected. Re-running it updates the existing
Release. The notes come from the tagged commit: fix a typo by editing
the Release on GitHub, and in CHANGELOG.md on `main`.

## What installs see

Each install checks for new release tags at most once a day and DMs
its owners about a new one. Nothing is applied until an owner asks
their Muse to update.

## Fixing a bad release

Ship a new patch release. Never move or delete a published tag:
installs may already have it, and owners would get different code
under one version.
