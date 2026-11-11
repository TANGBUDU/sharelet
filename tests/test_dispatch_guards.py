"""Exercise cancellation at the actual send boundary, after internal queue waits."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from host import HermesBridge
from jobs import ImageJobs
from media import MediaStore
from sharelet import CapsuleError, CapsuleManager

SCHEMA = {'type': 'object', 'properties': {'text': {'type': 'string', 'maxLength': 50}},
          'required': ['text'], 'additionalProperties': False}


class DispatchGuardTests(unittest.TestCase):
    def scenario(self, change):
        gate, entering = threading.Event(), threading.Event()
        calls = []
        class Context:
            plugin_id = 'sharelet'
            def get_config(self, key, default=None): return default
            def dispatch_tool(self, name, args):
                calls.append(args['text'])
                if args['text'] == 'held': gate.wait(10)
                return json.dumps({'value': args['text']})
        inventory = [{'name': 'mcp__files__read', 'toolset': 'mcp-files', 'schema': {'parameters': SCHEMA}}]
        configuration = {'plugins': {'entries': {'sharelet': {'mcp_allowlist': ['files']}}}, 'mcp_servers': {'files': {}}}
        bridge = HermesBridge(Context(), inventory=lambda: inventory, configuration=lambda: configuration, call_timeout=.5)
        clock, changed = [time.time()], [False]
        def dispatch(*args):
            entering.set()
            return bridge.call(*args)
        def require_tool(row):
            if changed[0]:
                raise CapsuleError('The reviewed tool changed. Ask the owner to create a new share. No run was charged.', 'tool_changed', 409)
        with tempfile.TemporaryDirectory() as directory:
            manager = CapsuleManager(Path(directory), dispatch, clock=lambda: clock[0])
            jobs = ImageJobs(manager, MediaStore(Path(directory)), require_tool=require_tool)
            try:
                for _ in range(4):
                    self.assertEqual(bridge.call('files', 'mcp__files__read', {'text': 'held'}), {'ok': False, 'uncertain': True})
                token = manager.create(title='Guarded call', server='files', tool='mcp__files__read', schema=SCHEMA, public_safe_approved=True)
                job = jobs.submit(token, {'text': 'waiting'}, 'h'*32)
                self.assertTrue(entering.wait(2))
                if change == 'revoke': manager.revoke(token)
                elif change == 'expire': clock[0] = manager.public_info(token)['expires_at'] + 1
                elif change == 'contract': changed[0] = True
                gate.set()
                jobs.executor.shutdown(wait=True)
                with manager._db() as db:
                    row = db.execute('SELECT state,error FROM image_jobs WHERE id=?', (job['id'],)).fetchone()
                self.assertNotIn('waiting', calls)
                self.assertEqual(row['state'], 'failed' if change == 'contract' else 'interrupted')
                self.assertIn('No run was charged', row['error'])
                self.assertEqual(manager.public_info(token)['calls'], 0)
                self.assertEqual(jobs.usage()['used'], 0)
            finally:
                gate.set()
                jobs.executor.shutdown(wait=True, cancel_futures=True)

    def test_revocation_while_waiting_prevents_dispatch_and_refunds(self): self.scenario('revoke')
    def test_expiry_while_waiting_prevents_dispatch_and_refunds(self): self.scenario('expire')
    def test_changed_tool_contract_while_waiting_prevents_dispatch_and_refunds(self): self.scenario('contract')
