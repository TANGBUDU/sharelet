"""Real MCP owner-controller round trip. Only local previews are permitted."""
import asyncio
import json
from pathlib import Path
import secrets
import sys
import tempfile
import time
import unittest
from urllib.request import Request, urlopen

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from owner_client import OwnerClient
from public_tools import basic_bindings, direct_dispatch
from service import Runtime

ROOT = Path(__file__).resolve().parents[1]


class ControllerTests(unittest.TestCase):
    def test_real_stdio_controller_requires_separate_owner_approval_before_running(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Runtime(folder, direct_dispatch, basic_bindings, connection={'mode': 'local'},
                              port=0, guest_port=0).start()
            try:
                async def flow():
                    params = StdioServerParameters(command=sys.executable,
                        args=[str(ROOT/'sharelet_mcp.py'), '--data-dir', folder, '--no-auto-start', '--local-only'])
                    async with stdio_client(params) as (read, write):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            names = {tool.name for tool in (await session.list_tools()).tools}
                            self.assertEqual(len(names), 5)
                            schema = next(tool.inputSchema for tool in (await session.list_tools()).tools if tool.name=='sharelet_create_share')
                            self.assertNotIn('public_safe_approved',schema['properties'])
                            async def call(name, arguments):
                                value = await session.call_tool(name, arguments)
                                self.assertFalse(value.isError, value)
                                return value.structuredContent or json.loads(value.content[0].text)
                            tools = await call('sharelet_list_tools', {})
                            self.assertEqual(len(tools['tools']), 2)
                            body = {'tool_id': tools['tools'][0]['id'], 'title': 'QA controller share',
                                    'public': False, 'max_calls': 1,
                                    'fixed_args': {'mode': 'upper', 'preserve_lines': False}}
                            denied = await call('sharelet_create_share', {**body, 'public': True})
                            self.assertFalse(denied['ok'])
                            created = await call('sharelet_create_share', body)
                            self.assertTrue(created['ok'])
                            self.assertNotIn('url',created)
                            listing = await call('sharelet_list_shares', {})
                            self.assertEqual(listing['shares'], [])
                            self.assertEqual(listing['drafts'][0]['state'],'pending')
                            return created['draft']
                draft = asyncio.run(flow())
                self.assertEqual(runtime.manager.list_shares(),[])
                created = OwnerClient(folder,auto_start=False).request('/api/owner/drafts/'+draft['id']+'/approve',
                    {'review_digest':draft['review_digest'],'public_safe_approved':True,'tool_review_hash':draft['tool_review_hash']})
                # The long-lived service survives the agent/controller process exiting.
                self.assertTrue(OwnerClient(folder, auto_start=False).request('/api/owner/ping')['ok'])
                base, slug = created['url'].split('/s/')
                payload = {'input': {'text': ' hello  controller '}, 'request_id': secrets.token_hex(16)}
                with urlopen(Request(base+'/api/s/'+slug+'/jobs', data=json.dumps(payload).encode(),
                                     headers={'Content-Type': 'application/json'}), timeout=5) as response:
                    job = json.load(response)['job']
                for _ in range(100):
                    with urlopen(base+'/api/s/'+slug+'/jobs/'+job['id'], timeout=5) as response:
                        job = json.load(response)['job']
                    if job['state'] not in ('queued', 'running'):
                        break
                    time.sleep(.02)
                self.assertEqual(job['result']['text'], 'HELLO CONTROLLER')
                self.assertEqual(runtime.jobs.budget()['used'], 0)
                async def revoke():
                    params = StdioServerParameters(command=sys.executable,
                        args=[str(ROOT/'sharelet_mcp.py'), '--data-dir', folder, '--no-auto-start', '--local-only'])
                    async with stdio_client(params) as (read, write):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            status = await session.call_tool('sharelet_connection_status', {})
                            self.assertFalse(status.isError)
                            status_value = status.structuredContent or json.loads(status.content[0].text)
                            self.assertEqual(status_value['usage']['used'], 1)
                            result = await session.call_tool('sharelet_revoke_share', {'share_id': created['id']})
                            self.assertFalse(result.isError)
                            result_value = result.structuredContent or json.loads(result.content[0].text)
                            self.assertTrue(result_value['ok'])
                asyncio.run(revoke())
                state = OwnerClient(folder, auto_start=False).request('/api/owner/state')
                self.assertEqual(state['shares'][0]['status'], 'revoked')
            finally:
                runtime.close()
                runtime.jobs.executor.shutdown(wait=True, cancel_futures=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
