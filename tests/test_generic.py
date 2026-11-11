"""Generic tool selection, scalar inputs and grant/revision boundaries."""
import contextvars
import json
from pathlib import Path
import secrets
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from catalog import tool_binding
from host import HermesBridge
from public_tools import basic_bindings, direct_dispatch
from service import Runtime
from sharelet import CapsuleError


class GenericHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.calls = []
        self.entries = basic_bindings()
        def dispatch(server, tool, arguments):
            self.calls.append((server, tool, arguments))
            return direct_dispatch(server, tool, arguments)
        self.runtime = Runtime(self.temp.name, dispatch, lambda: self.entries,
                               connection={'mode': 'test'}, port=0, guest_port=0).start()
        self.base = 'http://127.0.0.1:' + str(self.runtime.info['owner_port'])
        self.guest = 'http://127.0.0.1:' + str(self.runtime.info['guest_port'])
        self.owner = self.runtime.manager.owner_token

    def tearDown(self):
        self.runtime.close()
        self.runtime.jobs.executor.shutdown(wait=True, cancel_futures=True)
        self.temp.cleanup()

    def request(self, path, body=None, owner=False):
        try:
            result = urlopen(Request(self.base+path, data=None if body is None else json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json',
                                              **({'Authorization': 'Bearer '+self.owner} if owner else {})}), timeout=5)
        except HTTPError as error:
            result = error
        with result:
            return result.code, json.load(result)

    def create(self, **changes):
        body = {'tool_id': self.entries[0]['id'], 'title': 'Clean my text', 'max_calls': 3,
                'expires_seconds': 3600, 'public_safe_approved': True, 'public': False,
                'fixed_args': {'mode': 'upper'}}
        body.update(changes)
        status, value = self.request('/api/owner/shares', body, owner=True)
        self.assertEqual(status, 201, value)
        return value['url'].split('/s/')[1]

    def finish(self, slug, job):
        for _ in range(100):
            code, result = self.request('/api/s/'+slug+'/jobs/'+job['id'])
            self.assertEqual(code, 200, result)
            if result['job']['state'] not in ('queued', 'running'):
                return result['job']
            time.sleep(.01)
        self.fail('The controlled job did not finish')

    def test_non_image_tool_runs_without_an_image_key_and_recovers_same_result(self):
        with patch.dict('os.environ', {}, clear=True):
            slug = self.create()
            body = {'input': {'text': ' hello   world ', 'preserve_lines': False},
                    'request_id': secrets.token_hex(16)}
            code, response = self.request('/api/s/'+slug+'/jobs', body)
            self.assertEqual(code, 202, response)
            job = self.finish(slug, response['job'])
            self.assertEqual(job['kind'], 'tool')
            self.assertEqual(job['result']['text'], 'HELLO WORLD')
            self.assertNotIn('media_url', job)
            code, repeated = self.request('/api/s/'+slug+'/jobs', body)
            self.assertEqual(repeated['job']['id'], job['id'])
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(self.runtime.jobs.budget()['used'], 0)
            self.assertEqual(self.runtime.jobs.usage()['used'], 1)

    def test_fixed_argument_is_hidden_and_cannot_be_overridden(self):
        slug = self.create()
        _, metadata = self.request('/api/s/'+slug)
        self.assertNotIn('mode', metadata['share']['schema']['properties'])
        code, _ = self.request('/api/s/'+slug+'/jobs',
                               {'input': {'text': 'hello', 'mode': 'lower'}, 'request_id': secrets.token_hex(16)})
        self.assertEqual(code, 400)
        self.assertEqual(self.calls, [])

    def test_final_result_download_survives_exhaustion_but_is_bound_to_its_share(self):
        slug = self.create(max_calls=1)
        other = self.create()
        _, response = self.request('/api/s/'+slug+'/jobs',
                                   {'input': {'text': '<script>no</script>'}, 'request_id': secrets.token_hex(16)})
        job = self.finish(slug, response['job'])
        code, info = self.request('/api/s/'+slug)
        self.assertEqual((code, info['share']['status']), (200, 'exhausted'))
        code, downloaded = self.request('/api/s/'+slug+'/jobs/'+job['id']+'/result?download=1')
        self.assertEqual(code, 200)
        self.assertEqual(downloaded['text'], '<SCRIPT>NO</SCRIPT>')
        self.assertEqual(self.request('/api/s/'+other+'/jobs/'+job['id'])[0], 404)
        self.assertEqual(self.request('/api/s/'+other+'/jobs/'+job['id']+'/result')[0], 404)

    def test_unavailable_tool_is_rejected_before_charging(self):
        slug = self.create()
        self.entries = []
        code, result = self.request('/api/s/'+slug+'/jobs',
                                    {'input': {'text': 'hello'}, 'request_id': secrets.token_hex(16)})
        self.assertEqual((code, result['code']), (503, 'tool_unavailable'))
        self.assertEqual(self.runtime.jobs.usage()['used'], 0)

    def test_changed_tool_contract_requires_a_new_share_before_charging(self):
        slug = self.create()
        self.entries[0]['schema']['properties']['text']['maxLength'] = 50
        code, result = self.request('/api/s/'+slug+'/jobs',
                                    {'input': {'text': 'hello'}, 'request_id': secrets.token_hex(16)})
        self.assertEqual((code, result['code']), (409, 'tool_changed'))
        self.assertEqual(self.runtime.jobs.usage()['used'], 0)

    def test_typed_booleans_and_numbers_are_not_silently_coerced(self):
        slug = self.create()
        code, _ = self.request('/api/s/'+slug+'/jobs',
                               {'input': {'text': 'hello', 'preserve_lines': 'false'},
                                'request_id': secrets.token_hex(16)})
        self.assertEqual(code, 400)
        self.assertEqual(self.calls, [])

    def test_legacy_route_cannot_bypass_execution_allowance(self):
        slug = self.create()
        code, result = self.request('/api/s/'+slug, {'text': 'hello'})
        self.assertEqual((code, result['code']), (400, 'tool_job_required'))
        self.assertEqual(self.runtime.jobs.usage()['used'], 0)

    def test_generic_results_are_purged_on_revocation(self):
        slug = self.create()
        _, response = self.request('/api/s/'+slug+'/jobs',
                                   {'input': {'text': 'private result'}, 'request_id': secrets.token_hex(16)})
        job = self.finish(slug, response['job'])
        self.runtime.manager.revoke(slug)
        self.runtime.jobs.cleanup_media()
        with self.runtime.manager._db() as db:
            row = db.execute('SELECT result_json,artifact_purged FROM image_jobs WHERE id=?', (job['id'],)).fetchone()
        self.assertIsNone(row['result_json'])
        self.assertEqual(row['artifact_purged'], 1)
        self.assertEqual(self.request('/api/s/'+slug+'/jobs/'+job['id'])[0], 410)

    def test_execution_limit_covers_free_tools(self):
        self.runtime.jobs.execution_limit = 1
        slug = self.create()
        _, response = self.request('/api/s/'+slug+'/jobs',
                                   {'input': {'text': 'first'}, 'request_id': secrets.token_hex(16)})
        self.finish(slug, response['job'])
        code, result = self.request('/api/s/'+slug+'/jobs',
                                    {'input': {'text': 'second'}, 'request_id': secrets.token_hex(16)})
        self.assertEqual((code, result['code']), (429, 'execution_budget'))
        self.assertEqual(len(self.calls), 1)


class DiscoveryTests(unittest.TestCase):
    def make_bridge(self):
        self.invocations = []
        self.config = {'plugins': {'entries': {'sharelet': {'mcp_allowlist': ['approved']}}},
                       'mcp_servers': {'approved': {}, 'private': {}}}
        self.inventory = [
            {'name': 'mcp__approved__clean_name', 'toolset': 'mcp-approved',
             'schema': {'description': 'Clean a document', 'parameters': basic_bindings()[0]['schema']}},
            {'name': 'mcp__private__secret', 'toolset': 'mcp-private',
             'schema': {'parameters': basic_bindings()[0]['schema']}},
            {'name': 'mcp__approved__foreign', 'toolset': 'another-toolset',
             'schema': {'parameters': basic_bindings()[0]['schema']}},
        ]
        class Context:
            plugin_id = 'sharelet'
            def get_config(_, key, default=None): return default
            def dispatch_tool(_, name, arguments):
                self.invocations.append((name, arguments))
                return json.dumps({'result': {'answer': 'done'}})
            def call_mcp(_, *args, **kwargs):
                self.fail('Generic discovery must preserve the exact registered tool identity')
        return HermesBridge(Context(), inventory=lambda: self.inventory, configuration=lambda: self.config)

    def test_only_explicitly_granted_mcp_toolsets_are_discovered(self):
        bridge = self.make_bridge()
        rows = bridge.catalog()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['tool'], 'mcp__approved__clean_name')

    def test_generic_dispatch_uses_the_exact_registered_name(self):
        bridge = self.make_bridge()
        result = bridge.call('approved', 'mcp__approved__clean_name', {'text': 'hi'})
        self.assertTrue(result['ok'])
        self.assertEqual(self.invocations[0][0], 'mcp__approved__clean_name')

    def test_removing_a_server_grant_blocks_an_existing_binding(self):
        bridge = self.make_bridge()
        self.config['plugins']['entries']['sharelet']['mcp_allowlist'] = []
        with self.assertRaises(PermissionError):
            bridge.call('approved', 'mcp__approved__clean_name', {'text': 'hi'})
        self.assertEqual(self.invocations, [])

    def test_unsupported_schema_and_credential_fields_are_visible_but_not_publishable(self):
        rows = [
            tool_binding('sample', 'nested', {'type': 'object', 'properties': {'items': {'type': 'array', 'items': {'type': 'string'}}}}),
            tool_binding('sample', 'credential', {'type': 'object', 'properties': {'api_key': {'type': 'string'}}}),
            tool_binding('sample', 'erase', basic_bindings()[0]['schema'], annotations={'destructiveHint': True}),
        ]
        self.assertTrue(all(not row['supported'] and row['unsupported_reason'] for row in rows))

    def test_controller_tools_cannot_be_published(self):
        row = tool_binding('controller', 'sharelet_list_shares', {'type': 'object', 'properties': {}})
        self.assertFalse(row['supported'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
