# UPDATING.md: updating muse-discord

You, the owner's Muse, follow this guide with the owner. The goal: the
chosen release running, with nothing of theirs lost without their
say-so. Commands run in the package dir. Never print the bot token.

An update never repeats SETUP.md: `config.yaml`, `persona.md` and the
token stay as they are, and the owner does nothing in Discord. Run
`bin/connect-discord.py` again only when the token was rejected
(TROUBLESHOOTING.md).

## What belongs to the owner

Never discard these; updates never need to overwrite them:

- `config.yaml` (untracked) and its backups, `config.discord.yaml`
  (settings changed through the bot), the token file;
- `persona.md` (tracked as a placeholder; upstream never changes it);
- custom plugins: their own folders under `plugins/` (untracked);
- `worker-prompt.md` (rendered, untracked) and the registered hooks;
- runtime state: `state.json`, `mention_ledger.json`, `watches.json`,
  `proactive.json`, the queue and outbox, logs.

Any other tracked file the owner (or you for them) edited is a
**local modification** (step 3b). Steer new customizations to
`config.yaml`, `persona.md` or a plugin: those never conflict.

## Step 1: Pick the release, and look before touching anything

Releases are semver tags `vX.Y.Z` on origin, each with a `## vX.Y.Z
(date): title` CHANGELOG entry. Set `TAG` to:

- **From the bot's update DM**: the `version` in the wake payload
  (also in `update_request.json`).
- **By hand** ("update to the latest"): the newest release on origin,
  skipping pre-releases such as `v1.2.0-rc.1`:

  ```bash
  git ls-remote --tags --refs origin 'v*' | sed 's#.*refs/tags/##' \
    | grep -E '^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' \
    | sort -V | tail -1
  ```

  Nothing printed: there is no release yet; tell the owner and stop
  (or see "Tracking main instead").

Then:

```bash
TAG=vX.Y.Z                           # the release picked above
git fetch --no-tags origin tag "$TAG"
OLD=$(git rev-parse HEAD)            # keep this for steps 4 and 6
git describe --tags --match 'v[0-9]*' --abbrev=0 2>/dev/null \
  || echo unreleased                 # the version running now
git log --oneline HEAD.."$TAG"       # what is new
git diff HEAD "$TAG" -- CHANGELOG.md
git status --short --untracked-files=no
git log --oneline "$TAG"..HEAD       # local commits, if any
```

If `git merge-base --is-ancestor "$TAG" HEAD` succeeds, that release
is already installed: tell the owner and stop. Otherwise read the
CHANGELOG diff and note every **Action needed** item (step 4).

## Step 2: Which path

- **Clean**: `git status` lists nothing (or only `persona.md`) and
  there are no local commits: step 3a.
- **Customized**: anything else: step 3b.

## Step 3a: Clean install

```bash
git merge --ff-only "$TAG"
```

## Step 3b: Customized install

1. Show the owner what they changed and what the update touches:

   ```bash
   git diff --stat                      # their uncommitted changes
   git diff --stat HEAD "$TAG"          # the update
   ```

   Files in both may conflict. Explain it in plain words and get an
   explicit OK.

2. Make a safety net first:

   ```bash
   git branch "backup/pre-update-$(date +%Y%m%d-%H%M)"
   git diff > "$HOME/muse-discord-local-changes-$(date +%Y%m%d-%H%M).patch"
   ```

3. Apply the update on top of their changes:

   ```bash
   git stash push -m "local changes before update"
   git merge --ff-only "$TAG"
   # or, when there are local commits (a merge commit):
   # git merge --no-edit "$TAG"
   git stash pop
   ```

   Skip the stash lines when nothing is uncommitted. If git asks for
   an identity for the merge commit, set it for this repo only (`git
   config user.name`, `git config user.email`) with the owner's OK.

4. On a conflict, stop and go through it file by file with the owner:
   keep what their change was for, take the release's fixes, and ask
   when unsure. To back out: `git merge --abort` if a merge is in
   progress, then `git reset --keep "$OLD"` and `git stash pop`.

Never run `git reset --hard`, `git checkout -- <file>`, `git clean` or
`git stash drop` on the owner's work unless they ask for exactly that.

### Tracking main instead

To follow every commit on main, use `@{upstream}` where the steps say
`"$TAG"`: `git fetch` in step 1; `git pull --ff-only` (clean) or `git
pull --no-rebase` (local commits) in step 3. The update DM still
offers only releases, skipping any HEAD already contains.

## Step 4: Bring the rest along

- **Dependencies**: `bash bin/install.sh` (quick when nothing
  changed).
- **Config**: `git diff "$OLD" HEAD -- config.example.yaml` shows new
  settings. Each has a safe default, so the old `config.yaml` keeps
  working; mention each to the owner and add only what they want.
  Then run `python3 bin/settings.py validate` and fix what it reports.
- **Old settings layout**: see "Converting the settings layout" below.
- **Action needed** items: walk through each with the owner and do the
  ones they agree to.
- **Worker prompt**: render it again with the same `--media` as
  before: `python3 bin/render-worker-prompt.py --media
  <none|images|video|images,video>`. If the owner had asked for
  changes inside a seam, apply them to `worker-prompt.md` again. A wake
  hook that still carries the whole prompt inline (from before the
  renderer) gets the short wake prompt from SETUP.md step 5 instead.
- **Hooks**: every hook in SETUP.md step 5's table must be
  registered: register any that is missing, as the table says, and
  remove an old scheduler entry for `watchdog.sh` once
  `hooks/watchdog-hook.sh` runs. If the runtime registered a copy of a
  script that changed, update the copy.
- **Memory note**: if your memory has no note about this install,
  save the one from SETUP.md step 5.

### Converting the settings layout

An old-layout `config.yaml` (from before `bin/settings.py`) keeps
working: every old setting maps to its new name, with one log line.
Converting is optional, and only with the owner:

1. Tell them: the bot works as before; the new layout is shorter and
   needed for per-server and per-channel settings; the old file is
   kept as a backup. One thing changes either way: in servers the bot
   also answers the person it just replied to when they keep talking
   within about 2 minutes (to keep the old behaviour, set
   `all_servers.who.replies: when-addressed` after converting).
2. With their OK, run `python3 bin/settings.py migrate` (`--dry-run`
   previews). It writes the new `config.yaml`, keeps the old one as
   `config.yaml.bak-<UTC time>`, prints any change in behaviour, and
   refuses to write if anything but the conversational replies would
   change.
3. Show the owner the new file and what migrate printed, then run
   `python3 bin/settings.py validate`.
4. To undo, copy the backup back over `config.yaml`.

## Step 5: Restart

```bash
kill "$(cat monitor.pid)"
pkill -f "^python3 presence.py"
bash watchdog.sh
# expected: "OK monitor restarted" (and "OK presence restarted" if
# presence is enabled), or "OK monitor alive" when the wake hook
# restarted the monitor first
```

## Step 6: Check, then report

1. Run `python3 bin/doctor.py --no-dm` and fix what it reports.
2. Tell the owner briefly: the version now running (`git describe
   --tags`), what changed in a few sentences, which Action needed
   items were done, and anything still waiting on them.

If `update_request.json` exists (the update came from the update DM),
delete it now, or when you stop at an earlier step.

To roll back a misbehaving version, with the owner's OK: `git reset
--keep "$OLD"`, then restart as in step 5.
