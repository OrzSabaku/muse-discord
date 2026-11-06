#!/usr/bin/env python3
"""Plan and execute registered app actions with a live owner-DM binding."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from muse_discord.external_apps.broker import Broker
from muse_discord.external_apps.policy import Refused, load_policy


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--for', dest='for_id')
    parser.add_argument('--nonce')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('validate')
    plan = commands.add_parser('plan')
    plan.add_argument('app')
    plan.add_argument('tool')
    plan.add_argument('--arguments', default='{}', help='JSON object; use - to read stdin')
    execute = commands.add_parser('execute')
    execute.add_argument('request_id')
    execute.add_argument('--receipt', help='Signed receipt JSON; use - to read stdin')
    args = parser.parse_args(argv)
    try:
        if args.command == 'validate':
            policy = load_policy(ROOT / 'external-apps.yaml')
            result = {'valid': True, 'apps': sorted(policy['apps'])}
        else:
            broker = Broker(ROOT, os.environ.get('MUSE_DISCORD_CONFIG'))
            if args.command == 'plan':
                raw = sys.stdin.read(65537) if args.arguments == '-' else args.arguments
                if len(raw.encode()) > 65536:
                    raise Refused('INVALID_ARGUMENTS')
                result = broker.plan(args.for_id, args.nonce, args.app, args.tool, json.loads(raw))
            else:
                receipt = args.receipt
                if receipt == '-':
                    receipt = sys.stdin.read(65537)
                if receipt is not None and len(receipt.encode()) > 65536:
                    raise Refused('INVALID_APPROVAL')
                result = broker.execute(args.for_id, args.nonce, args.request_id,
                                        json.loads(receipt) if receipt else None)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except Refused as error:
        code = error.code
    except (ValueError, TypeError, OSError, sqlite3.Error):
        code = 'INVALID_INPUT_OR_STATE'
    print(json.dumps({'error': {'code': code}}))
    return 1


if __name__ == '__main__':
    sys.exit(main())
