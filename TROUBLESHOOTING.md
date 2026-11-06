# TROUBLESHOOTING.md: when muse-discord misbehaves

For the owner's Muse. Start with `python3 bin/doctor.py --no-dm`: it
names each problem with its fix. Below: what its problems and the
watchdog's alerts mean, and checks to run by hand.

To restart the bot: `kill "$(cat monitor.pid)"; bash watchdog.sh`.
To stop it: unregister the wake hook and the watchdog hook (either
restarts a dead monitor), then `kill "$(cat monitor.pid)"` and `pkill
-f "^python3 presence.py"`.

## What a problem means

- **The bot is not running**: run `bash watchdog.sh`. If it still
  fails, read the top of `monitor.log` (the previous run's is
  `monitor.log.1`): a Python error usually means missing packages, so
  run `bash bin/install.sh`.
- **`TOKEN REJECTED` / "Discord rejected the token"**: the token was
  reset in the Developer Portal. Ask the owner for a fresh one (Bot
  page, **Reset Token**, **Copy**) and run `bin/connect-discord.py
  --token-file` as in SETUP.md step 3. The next watchdog run starts
  the bot (the wake hook never retries a rejected token).
- **`GATEWAY DISABLED (code 4014)` / "Message Content is off"**: the
  bot can't see replies, follow-ups or replies to itself. Run
  `python3 bin/connect-discord.py --saved` (it turns the intent on, or
  prints an `OWNER STEP` to send the owner), then restart.
- **The bot looks stuck**: its main loop stopped touching
  `monitor.heartbeat` for 10+ minutes; the watchdog restarts it (the
  stuck run's log is `monitor.log.1`).
- **It can't see any channels**: it was not invited, or its role lacks
  View Channels. `python3 bin/connect-discord.py --saved` prints the
  invite link again (`INVITE:`). To keep it out of a channel, deny it
  View Channel there.
- **It can't post where it was mentioned**: give its role Send
  Messages in that channel.
- **A hook "has never run" or "last ran N minutes ago"**: register it
  again as SETUP.md step 5's table says (each hook touches a
  `.seen-<name>` file in the package dir when it runs).
- **The queue fills but nobody answers**: the wake hook isn't waking a
  worker, or its wake prompt doesn't point at this package's
  `worker-prompt.md`. An item is handed out again 10 minutes after its
  last wake.
- **`Skipping duplicate reply` in monitor.log**: two workers drafted
  the same message (two registered wake hooks, or a hook plus a
  poller). Only one reply goes out; keep one waker.
- **`WORKER PROMPT OUT OF DATE`**: render it again (UPDATING.md step
  4, "Worker prompt").
- **Couldn't DM the owner**: they block DMs from server members. In
  Discord: right-click the server icon, **Privacy Settings**, switch on
  **Direct Messages**.
- **`/settings` doesn't show up**: monitor.log says `/settings command
  created` (or `updated`) when it registers it, or `registration
  failed` (retried every minute). Restarting the Discord app helps; a
  server admin may have hidden it under Server Settings, Integrations.
  "Settings UI unavailable" means `settings_ui.py` is missing or
  failed to load (monitor.log says why).
- **The invite link fails**: on the Bot page, **Requires OAuth2 Code
  Grant** must be off; `bin/connect-discord.py` reports it.
- **Buttons and `/settings` do nothing**: the app's **Interactions
  Endpoint URL** (General Information page) must be empty.
- **Pin requests fail**: a bot invited before February 2026 lacks Pin
  Messages. In Server Settings, Roles, the bot's role, switch on **Pin
  Messages** (or invite it again).
- **It ignores a server it was added to**: none of the owners is in
  that server (or the check hasn't answered yet), so it answers only
  the owners there, and the server doesn't count as shared for DMs (a
  stranger may have added a public bot). The owner's join DM says so;
  to let it talk there, they press **Configure** and choose When it
  answers.
- **connect-discord.py exits 6**: `config.yaml` doesn't validate, and
  the script won't touch it (the running bot keeps its last good
  settings). `python3 bin/settings.py validate` names each problem.

## Keeping the token somewhere else

`token_from` in config.yaml can name another file, or `env:NAME` for
an environment variable. A variable must be set wherever the watchdog
and the wake hook run; with only the variable, only the scheduled
watchdog run can restart the monitor.

## Checks by hand

After a big change, with the owner's help where noted.
`mention_ledger.json` records how every message that addressed the bot
ended.

1. **Replies and follow-ups** (owner): reply to a bot message without
   a mention: it answers. Within 2 minutes, write again with no
   mention: it answers that too. Someone else's message doesn't count.
2. **A channel it can read but not post in**: a mention there gets no
   reply, one `Skipping replies in ...` log line and a `skipped` ledger
   entry.
3. **Restart catch-up**: `kill "$(cat monitor.pid)"` and mention the
   bot at once. The wake hook restarts it within ~10 s (`run triggered
   by the wake hook` in watchdog.log) and the reply follows.
4. **Sweep**: every ~3 minutes it rereads recent history, and logs
   `Sweep recovered lost mention ...` only when no record accounts for
   one; silence is normal.
5. **Rate limits**: during a Discord 429 storm monitor.log shows one
   `RATE LIMITED: backing off REST for Ns.` line per storm while the
   gateway keeps delivering. Restarting extends the limit.
6. **Images and video** (owner, when the runtime can make them): ask
   for an image; the reply carries it, from `generated/<message id>/`.
   Someone not in `media.who` gets a brief no.
7. **A DM from someone else** (second account): no answer (DMs are
   owners-only by default).
8. **Another server the owner is in**: invite the bot; the owner gets
   a DM naming the server and its channels, and a mention there is
   answered.
9. **Another bot**: have it mention this bot; the answer reaches it as
   a real mention, and the exchange winds down instead of looping.
