"""Immutable, expiring plans and one-use execution with live DM authorization."""
import base64
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time

from muse_discord import muse_settings as MS
from .policy import LEVELS, Refused, canonical, digest, evaluate, load_policy

PLAN_TTL = 300
APPROVAL_FIELDS = ('request_id', 'for_id', 'owner_id', 'channel_id', 'app',
                   'tool', 'arguments', 'effect', 'operation_digest',
                   'policy_digest', 'plan_expires_at')


def _read(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        raise Refused('SOURCE_UNAVAILABLE') from None


def authorize(root, settings, for_id, nonce):
    if not re.fullmatch(r'[1-9][0-9]{16,19}', for_id or '') or not isinstance(nonce, str) or not nonce:
        raise Refused('OWNER_DM_REQUIRED')
    nonces = _read(root / 'owner_dm_nonces.json')
    want = nonces.get(for_id) if isinstance(nonces, dict) else None
    if not isinstance(want, str) or not hmac.compare_digest(want.encode(), nonce.encode()):
        raise Refused('OWNER_DM_REQUIRED')
    queue = _read(root / 'reply_queue.json')
    if not isinstance(queue, list):
        raise Refused('OWNER_DM_REQUIRED')
    item = next((q for q in queue if isinstance(q, dict) and q.get('message_id') == for_id), None)
    if (not item or item.get('kind') != 'dm' or item.get('from_bot') is not False
            or str(item.get('author_id')) not in settings.owners
            or not isinstance(item.get('external_apps'), dict)
            or item['external_apps'].get('eligible') is not True
            or item['external_apps'].get('level') not in LEVELS[1:]
            or not re.fullmatch(r'[1-9][0-9]{16,19}', str(item.get('channel_id', '')))):
        raise Refused('OWNER_DM_REQUIRED')
    return {'for_id': for_id, 'owner_id': str(item['author_id']),
            'channel_id': str(item['channel_id']),
            'source_level': item['external_apps']['level'],
            'nonce_hash': hashlib.sha256(nonce.encode()).hexdigest()}


@contextmanager
def store(root):
    path = root / 'external-apps.sqlite'
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)
    db = sqlite3.connect(path, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        db.execute('CREATE TABLE IF NOT EXISTS requests '
                   '(id TEXT PRIMARY KEY, body TEXT NOT NULL, status TEXT NOT NULL, '
                   'created REAL NOT NULL, expires REAL NOT NULL)')
        # Minimize retention of arguments; no payload is written to audit logs.
        db.execute('DELETE FROM requests WHERE expires < ?', (time.time() - 86400,))
        db.commit()
        yield db
    finally:
        db.close()


def verify_receipt(policy, expected, receipt, now):
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.exceptions import InvalidSignature
    except ImportError:
        raise Refused('APPROVAL_VERIFIER_UNAVAILABLE') from None
    if not policy.get('approval_public_key'):
        raise Refused('APPROVAL_UNAVAILABLE')
    if not isinstance(receipt, dict) or set(receipt) != {'payload', 'signature'}:
        raise Refused('APPROVAL_REQUIRED')
    payload = receipt['payload']
    # All claims must match the exact plan. The authority may shorten its life.
    if (not isinstance(payload, dict) or set(payload) != set(expected) | {'expires_at'}
            or any(canonical(payload.get(k)) != canonical(v)
                   for k, v in expected.items())
            or type(payload.get('expires_at')) not in (int, float)
            or not now < payload['expires_at'] <= expected['plan_expires_at']):
        raise Refused('INVALID_APPROVAL')
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(policy['approval_public_key'], validate=True))
        key.verify(base64.b64decode(receipt['signature'], validate=True), canonical(payload).encode())
    except (ValueError, TypeError, InvalidSignature):
        raise Refused('INVALID_APPROVAL') from None


class Broker:
    def __init__(self, root, config=None, executor=None, clock=time.time):
        self.root = Path(root)
        self.config = Path(config) if config else self.root / 'config.yaml'
        self.executor, self.clock = executor, clock

    def context(self, for_id, nonce):
        try:
            settings = MS.load(str(self.config))
            level = settings.resolve(MS.Scope.everywhere())['abilities.external_apps']
        except (MS.SettingsError, OSError, ValueError):
            raise Refused('INVALID_SETTINGS') from None
        if level == 'off':
            raise Refused('POLICY_DENIED')
        source = authorize(self.root, settings, for_id, nonce)
        policy = load_policy(self.root / 'external-apps.yaml')
        # Raising policy later cannot increase an already queued DM's powers.
        level = LEVELS[min(LEVELS.index(level), LEVELS.index(source['source_level']))]
        return source, policy, level

    def plan(self, for_id, nonce, app_id, tool, arguments):
        source, policy, level = self.context(for_id, nonce)
        _app, effect, approval = evaluate(policy, level, app_id, tool, arguments)
        now = self.clock()
        request = {'request_id': secrets.token_hex(16), **source, 'app': app_id,
                   'tool': tool, 'arguments': arguments, 'effect': effect,
                   'approval_required': approval, 'policy_digest': digest({'policy': policy, 'level': level}),
                   'plan_expires_at': now + PLAN_TTL}
        request['operation_digest'] = digest(request)
        with store(self.root) as db:
            db.execute('INSERT INTO requests VALUES (?,?,?,?,?)',
                       (request['request_id'], canonical(request), 'pending', now, request['plan_expires_at']))
            db.commit()
        claims = {k: request[k] for k in APPROVAL_FIELDS}
        return {'request_id': request['request_id'], 'decision': 'approval-required' if approval else 'allowed',
                'channel_id': source['channel_id'], 'app': app_id, 'tool': tool,
                'arguments': arguments, 'approval_claims': claims,
                'expires_at': request['plan_expires_at']}

    def execute(self, for_id, nonce, request_id, receipt=None):
        if not isinstance(request_id, str) or not re.fullmatch(r'[a-f0-9]{32}', request_id):
            raise Refused('INVALID_REQUEST_ID')
        source, policy, level = self.context(for_id, nonce)
        with store(self.root) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM requests WHERE id=?', (request_id,)).fetchone()
            if not row or row['status'] != 'pending':
                raise Refused('REQUEST_NOT_PENDING')
            request = json.loads(row['body'])
            if any(request.get(k) != v for k, v in source.items()):
                raise Refused('SOURCE_CHANGED')
            if self.clock() >= row['expires']:
                raise Refused('REQUEST_EXPIRED')
            if request['policy_digest'] != digest({'policy': policy, 'level': level}):
                raise Refused('POLICY_CHANGED')
            app, _effect, approval = evaluate(policy, level, request['app'], request['tool'], request['arguments'])
            if approval:
                expected = {k: request[k] for k in APPROVAL_FIELDS}
                verify_receipt(policy, expected, receipt, self.clock())
            db.execute('UPDATE requests SET status=? WHERE id=?', ('running', request_id))
            db.commit()
        # Consume before invoking: crashes and uncertain remote results never
        # re-run a write automatically, including after a process restart.
        try:
            if self.executor is None:
                from .transport import call_tool
                output = call_tool(app, request['tool'], request['arguments'], self.root)
            else:
                output = self.executor(app, request['tool'], request['arguments'])
        except Exception:
            with store(self.root) as db:
                db.execute('UPDATE requests SET status=? WHERE id=?', ('unknown', request_id))
                db.commit()
            raise Refused('EXECUTION_UNCERTAIN') from None
        with store(self.root) as db:
            db.execute('UPDATE requests SET status=? WHERE id=?', ('done', request_id))
            db.commit()
        return {'request_id': request_id, 'channel_id': source['channel_id'],
                'untrusted': True, 'result': output}
