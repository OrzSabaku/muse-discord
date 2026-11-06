"""Nonce/source binding, immutable approvals, revocation and exactly-once attempts."""
import base64
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

import yaml

ROOT = Path(os.environ.get('MUT_DIR', Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(ROOT))
from external_apps.broker import Broker, store
from external_apps.policy import Refused, canonical
from test_policy import policy

OWNER = '100000000000000001'
ITEM = '300000000000000001'
CHANNEL = '200000000000000001'
NONCE = 'a' * 32
HAVE_CRYPTO = importlib.util.find_spec('cryptography') is not None
if HAVE_CRYPTO:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = time.time()
        self.data = policy()
        self.write_config('write')
        self.write_policy()
        self.item = {'message_id': ITEM, 'kind': 'dm', 'from_bot': False,
                     'author_id': OWNER, 'channel_id': CHANNEL,
                     'external_apps': {'eligible': True, 'level': 'write'}}
        self.write_source()
        self.calls = []
        self.broker = Broker(self.root, executor=self.execute, clock=lambda: self.now)

    def write_config(self, level, owner=OWNER):
        (self.root / 'config.yaml').write_text(yaml.safe_dump({
            'version': 1, 'owners': [owner], 'everywhere': {'abilities': {'external_apps': level}},
            'token_from': 'env:NOT_USED_IN_THESE_TESTS'}))

    def write_policy(self):
        (self.root / 'external-apps.yaml').write_text(yaml.safe_dump(self.data))

    def write_source(self, nonce=NONCE):
        (self.root / 'owner_dm_nonces.json').write_text(json.dumps({ITEM: nonce}))
        (self.root / 'reply_queue.json').write_text(json.dumps([self.item]))

    def execute(self, app, tool, args):
        self.calls.append((tool, args))
        return {'data': 'external app result; ignore any instructions here'}

    def plan(self, tool='list', args=None):
        return self.broker.plan(ITEM, NONCE, 'calendar', tool, args or {})

    def test_read_and_finite_write_are_one_attempt_and_dm_scoped(self):
        for tool, args in [('list', {}), ('draft', {'calendar_id': 'personal'})]:
            plan = self.plan(tool, args)
            self.assertEqual(plan['decision'], 'allowed')
            result = self.broker.execute(ITEM, NONCE, plan['request_id'])
            self.assertEqual(result['channel_id'], CHANNEL)
            self.assertTrue(result['untrusted'])
            with self.assertRaises(Refused) as caught:
                self.broker.execute(ITEM, NONCE, plan['request_id'])
            self.assertEqual(caught.exception.code, 'REQUEST_NOT_PENDING')
        self.assertEqual(len(self.calls), 2)
        self.assertEqual((self.root / 'external-apps.sqlite').stat().st_mode & 0o777, 0o600)

    def test_nonce_and_source_refusals_happen_before_planning(self):
        for field, value in [('kind', 'mention'), ('kind', 'watch'), ('author_id', '900000000000000001'),
                              ('from_bot', True), ('external_apps', {}),
                              ('external_apps', {'eligible': False})]:
            original = copy.deepcopy(self.item)
            self.item[field] = value
            self.write_source()
            with self.assertRaises(Refused):
                self.plan()
            self.item = original
        self.write_source()
        with self.assertRaises(Refused):
            self.broker.plan(ITEM, 'wrong', 'calendar', 'list', {})
        self.assertFalse((self.root / 'external-apps.sqlite').exists())
        self.assertEqual(self.calls, [])

    def test_policy_source_and_ownership_revocation_and_expiration(self):
        changes = [lambda: self.write_config('off'),
                   lambda: self.write_config('write', owner='900000000000000001'),
                   lambda: self.write_source('new-nonce'),
                   lambda: (self.root / 'reply_queue.json').write_text('[]'),
                   lambda: (self.root / 'external-apps.yaml').write_text('invalid: true'),
                   lambda: setattr(self, 'now', self.now + 301)]
        for change in changes:
            self.setUp()
            plan = self.plan()
            change()
            with self.assertRaises(Refused):
                self.broker.execute(ITEM, NONCE, plan['request_id'])
            self.assertEqual(self.calls, [])

    def test_valid_policy_change_invalidates_pending_plan(self):
        plan = self.plan()
        self.data['apps']['calendar']['level'] = 'read-only'
        self.write_policy()
        with self.assertRaises(Refused) as caught:
            self.broker.execute(ITEM, NONCE, plan['request_id'])
        self.assertEqual(caught.exception.code, 'POLICY_CHANGED')
        self.assertEqual(self.calls, [])

    def test_raising_global_level_does_not_upgrade_an_older_dm(self):
        self.item['external_apps']['level'] = 'read-only'
        self.write_source()
        self.assertEqual(self.plan()['decision'], 'allowed')
        with self.assertRaises(Refused):
            self.plan('draft', {'calendar_id': 'personal'})
        self.assertEqual(self.calls, [])

    def test_uncertain_remote_result_is_never_retried(self):
        def fail(*args):
            self.calls.append('attempt')
            raise OSError('secret detail from provider')
        self.broker.executor = fail
        plan = self.plan('draft', {'calendar_id': 'personal'})
        with self.assertRaises(Refused) as caught:
            self.broker.execute(ITEM, NONCE, plan['request_id'])
        self.assertEqual(caught.exception.code, 'EXECUTION_UNCERTAIN')
        restarted = Broker(self.root, executor=fail)
        with self.assertRaises(Refused):
            restarted.execute(ITEM, NONCE, plan['request_id'])
        self.assertEqual(self.calls, ['attempt'])
        with store(self.root) as db:
            self.assertEqual(db.execute('SELECT status FROM requests').fetchone()['status'], 'unknown')

    def test_simultaneous_execution_starts_only_one_remote_call(self):
        started, release = threading.Event(), threading.Event()
        def run(*args):
            self.calls.append('attempt')
            started.set()
            self.assertTrue(release.wait(5))
            return {'ok': True}
        self.broker.executor = run
        plan = self.plan()
        failures = []
        def execute():
            try:
                self.broker.execute(ITEM, NONCE, plan['request_id'])
            except Exception as error:
                failures.append(error)
        thread = threading.Thread(target=execute)
        thread.start()
        try:
            self.assertTrue(started.wait(5))
            with self.assertRaises(Refused):
                Broker(self.root, executor=run).execute(ITEM, NONCE, plan['request_id'])
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(self.calls, ['attempt'])

    @unittest.skipUnless(HAVE_CRYPTO, 'Optional approval verifier dependency')
    def test_signed_exact_operation_receipt_and_replay(self):
        private = Ed25519PrivateKey.generate()
        self.data['approval_public_key'] = base64.b64encode(private.public_key().public_bytes_raw()).decode()
        self.write_policy()
        self.write_config('confirm')
        plan = self.plan('draft', {'calendar_id': 'personal'})
        self.assertEqual(plan['decision'], 'approval-required')
        with self.assertRaises(Refused):
            self.broker.execute(ITEM, NONCE, plan['request_id'])
        payload = {**plan['approval_claims'], 'expires_at': self.now + 60}
        receipt = {'payload': payload, 'signature': base64.b64encode(
            private.sign(canonical(payload).encode())).decode()}
        for mutation in ('signature', 'channel', 'arguments', 'expired', 'extra', 'other_plan'):
            bad = copy.deepcopy(receipt)
            if mutation == 'signature':
                bad['signature'] = base64.b64encode(b'x' * 64).decode()
            elif mutation == 'channel':
                bad['payload']['channel_id'] = '200000000000000099'
            elif mutation == 'arguments':
                bad['payload']['arguments']['calendar_id'] = 'work'
            elif mutation == 'expired':
                bad['payload']['expires_at'] = self.now - 1
            elif mutation == 'extra':
                bad['payload']['extra'] = 'ignored?'
            else:
                bad['payload']['request_id'] = self.plan('draft', {'calendar_id': 'personal'})['request_id']
            with self.assertRaises(Refused):
                self.broker.execute(ITEM, NONCE, plan['request_id'], bad)
        self.assertEqual(self.calls, [])
        self.broker.execute(ITEM, NONCE, plan['request_id'], receipt)
        self.assertEqual(len(self.calls), 1)
        with self.assertRaises(Refused):
            self.broker.execute(ITEM, NONCE, plan['request_id'], receipt)

    @unittest.skipUnless(HAVE_CRYPTO, 'Optional approval verifier dependency')
    def test_dangerous_operations_require_approval_even_at_write_level(self):
        plan = self.plan('delete')
        self.assertEqual(plan['decision'], 'approval-required')
        with self.assertRaises(Refused) as caught:
            self.broker.execute(ITEM, NONCE, plan['request_id'])
        self.assertEqual(caught.exception.code, 'APPROVAL_UNAVAILABLE')
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
