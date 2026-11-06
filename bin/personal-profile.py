#!/usr/bin/env python3
"""Apply an owner-only DM profile, before starting the monitor."""
import argparse
import datetime
import os
from pathlib import Path
import re
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import yaml
import muse_settings as MS


def profile(owner, token_from):
    if not re.fullmatch(r'[1-9][0-9]{16,19}', owner):
        raise ValueError('owner must be your Discord user ID (17–20 digits)')
    return {
        'version': 1, 'owners': [owner], 'persona': 'persona.md',
        'token_from': token_from,
        'updates': {'notify': 'off'},
        'telemetry': {'usage_stats': 'off'},
        'memory': {'save_dms': 'on'},
        'everywhere': {
            'who': {'allow': [owner], 'bots': 'ignore'},
            'style': {'notes': '日本語で応答する。外部操作の承認はMuse本体の正規画面で行う。'},
            'privacy': {'memory': 'nothing', 'suspicious': 'hold-for-review'},
            'abilities': {'owner_commands': 'off', 'reminders': 'owners', 'external_apps': 'off'},
            'proactive': {'enabled': 'off', 'timezone': 'Asia/Tokyo'},
            'media': {'enabled': 'off'},
        },
        'dms': {'who': {'dms_from': 'owners', 'replies': 'conversational'}},
        'all_servers': {'who': {'replies': 'off'}},
    }


def apply(directory, owner=None):
    directory = Path(directory)
    target = directory / 'config.yaml'
    overlay = directory / 'config.discord.yaml'
    # Refuse instead of silently inheriting wider permissions from an overlay.
    if overlay.exists():
        raise ValueError('config.discord.yaml exists: stop the monitor and archive it before applying')
    old = yaml.safe_load(target.read_text()) if target.exists() else {}
    old = old or {}
    owners = old.get('owners', [])
    if owner is None:
        if not isinstance(owners, list) or len(owners) != 1:
            raise ValueError('run connect-discord.py first, or pass --owner YOUR_USER_ID')
        owner = str(owners[0])
    token_from = old.get('token_from', '~/.config/muse-discord/bot_token')
    data = profile(owner, token_from)
    fd, name = tempfile.mkstemp(prefix='.personal-', suffix='.yaml', dir=directory)
    staged = Path(name)
    try:
        with os.fdopen(fd, 'w') as out:
            yaml.safe_dump(data, out, allow_unicode=True, sort_keys=False)
        MS.load(str(staged), str(directory / 'plugins'))
        if target.exists():
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            backup = directory / ('config.yaml.bak-personal-' + stamp)
            with backup.open('x') as out:
                os.chmod(backup, 0o600)
                out.write(target.read_text())
        os.replace(staged, target)
    finally:
        staged.unlink(missing_ok=True)
    return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owner', help='Discord user ID; inferred after connect-discord.py')
    args = parser.parse_args()
    try:
        print('Profile saved:', apply(ROOT, args.owner))
        print('Owner DM only; server replies, telemetry and update notices disabled.')
    except (ValueError, MS.SettingsError) as exc:
        print('Profile not applied:', exc, file=sys.stderr)
        sys.exit(1)
