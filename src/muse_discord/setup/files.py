"""Persist a verified connection while preserving owner configuration and persona.

Existing configuration is validated and backed up before replacement. Only the
shipped persona placeholder is replaced; private token files use atomic writes.
"""

import hashlib
import os
import re

from muse_discord.setup import support as C
from muse_discord.setup.credentials import DEFAULT_TOKEN_FILE
from muse_discord.storage import atomic_write

PKG = C.PKG_DIR

SHIPPED_PERSONAS = {
    "5798df97daabe50c99d3fc4e6b122aa135f3abd26ca00cefefce4c502d99a1f8",
}


DEFAULT_PERSONA = """\
# Persona

<!-- The bot's personality. By default the bot is simply your Muse,
     written here by setup (bin/connect-discord.py). To shape it, DM the
     bot in plain words ("keep replies short", "no swearing in the work
     server"), or ask your Muse in its main chat to edit this file. The
     worker re-reads it on every wake. -->

Your name is {name}.

You are your owner's Muse, the same one they talk with in your main
chat. Be yourself here: the same personality, voice and humor. In
their Discord servers you are also talking with other people, so:

- Answer what you're asked, directly; say so briefly when you don't
  know something.
- Keep to Discord's register: short to medium messages, light humor
  when it lands naturally, never forced.
- Stay calm and friendly if someone is rude.
- You are a guest in these communities: be a good community member
  first, a bot second.
"""


def write_private(path, text):
    """Write text to path, mode 600 from the start, atomically."""
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    atomic_write(path, None, 0o600, text=text)


def fresh_config_text(owner_id):
    with open(os.path.join(PKG, "config.example.yaml")) as f:
        text = f.read()
    new, n = re.subn(r"^owners: \[\]", f'owners: ["{owner_id}"]', text, count=1, flags=re.MULTILINE)
    if n != 1:
        raise RuntimeError("config.example.yaml has no 'owners: []' line")
    return new


def write_config(cfg, owner_id, rep):
    """config.yaml with owner_id among the owners. An existing valid file
    that already lists them is left alone; anything else is backed up
    before it is replaced. Returns the loaded settings."""
    MS = C.settings_module()
    from muse_discord.storage import atomic_write_text

    text = None
    if os.path.exists(cfg):
        try:
            st = C.load_settings(cfg)
        except MS.SettingsError:
            st = None
        if st is None:  # checked before anything was written
            raise MS.SettingsError("config.yaml is not valid")
        if owner_id in st.owners:
            rep.line("OK: Kept the existing config.yaml (it already lists the app's owner).")
            return MS, st
        bak = MS.backup(cfg)
        import yaml

        with open(cfg) as f:
            raw = yaml.safe_load(f) or {}
        before = [str(o) for o in raw.get("owners") or []]
        raw["owners"] = [*before, owner_id]
        text = MS.dump_config(raw)
        rep.line(
            f"NOTE: config.yaml listed {', '.join(before) or 'no owner'}"
            f"; added Discord user {owner_id} (this app's owner) as an "
            f"owner too. Tell the owner, and remove whoever shouldn't "
            f"be there. The old file is saved as "
            f"{os.path.basename(bak)}."
        )
    if text is None:
        text = fresh_config_text(owner_id)
        if not os.path.exists(cfg):
            rep.line(
                f"OK: Wrote config.yaml (owner: Discord user "
                f"{owner_id}); everything else is the default."
            )
    # Check it exactly as the bot will, before it replaces anything.
    MS.load(cfg, text=text)
    atomic_write_text(cfg, text)
    return MS, C.load_settings(cfg)


def name_persona(st, name, rep):
    """Write the default persona (the Muse itself, by name) while
    persona.md is still the shipped placeholder."""
    path = os.path.join(PKG, os.path.expanduser(st.persona or "persona.md"))
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        raw = None
    if raw is not None and hashlib.sha256(raw).hexdigest() not in SHIPPED_PERSONAS:
        rep.line("OK: Left persona.md as it is (it is already yours).")
        return
    safe = " ".join(name.split())[:80]
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(DEFAULT_PERSONA.format(name=safe))
    os.replace(tmp, path)
    rep.line(f"OK: persona.md: the bot is {safe}, being itself.")


def config_problem(cfg):
    """Why an existing config.yaml can't be used as is, or None."""
    if not os.path.exists(cfg):
        return None
    MS = C.settings_module()
    try:
        C.load_settings(cfg)
    except MS.SettingsError as e:
        return str(e)
    return None


def save_connection(args, token, owner_id, rep, name):
    # Save: token, config, persona.
    cfg = args.config
    MS = C.settings_module()
    path = None
    if os.path.exists(cfg):
        try:
            path = C.token_file(C.load_settings(cfg))
        except MS.SettingsError:
            path = None
    path = path or os.path.expanduser(DEFAULT_TOKEN_FILE)
    write_private(path, token + "\n")
    rep.line(f"OK: Saved the token in {path} (only this account can read it).")
    try:
        MS, st = write_config(cfg, owner_id, rep)
    except (MS.SettingsError, RuntimeError, OSError) as e:
        return rep.fail("config", f"Could not write config.yaml (the token is saved): {e}", 6)
    if C.token_file(st) != path:
        rep.line(f"NOTE: config.yaml reads the token from {st.token_sources()}, not {path}.")
    name_persona(st, name, rep)
