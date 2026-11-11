"""Review regressions: agent self-approval, dangerous tools and stale owner decisions."""
import json
from pathlib import Path
import secrets
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from catalog import tool_binding
from host import HermesBridge
from owner_client import OwnerClient
from public_tools import basic_bindings, direct_dispatch
from service import Runtime
from sharelet import CapsuleError


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.entries = basic_bindings()
        self.calls = []
        def dispatch(server,tool,args):
            self.calls.append((server,tool,args))
            return direct_dispatch(server,tool,args) if server=='sharelet_public' else {'ok':True,'result':args}
        self.runtime = Runtime(self.temp.name,dispatch,lambda:self.entries,connection={'mode':'test'},port=0,guest_port=0).start()
        self.base='http://127.0.0.1:'+str(self.runtime.info['owner_port'])
        self.guest='http://127.0.0.1:'+str(self.runtime.info['guest_port'])

    def tearDown(self):
        self.runtime.close();self.runtime.jobs.executor.shutdown(wait=True,cancel_futures=True)
        self.temp.cleanup()

    def request(self,path,body=None,role='owner',guest=False):
        key=self.runtime.manager.owner_token if role=='owner' else self.runtime.manager.agent_token
        try:
            response=urlopen(Request((self.guest if guest else self.base)+path,
                data=None if body is None else json.dumps(body).encode(),
                headers={'Content-Type':'application/json','Authorization':'Bearer '+key}),timeout=5)
        except HTTPError as error:response=error
        with response:return response.code,json.load(response)

    def body(self,**changes):
        return {'tool_id':self.entries[0]['id'],'title':'Controlled request','public':False,
                'max_calls':2,'fixed_args':{'mode':'upper'},**changes}

    def draft(self,**changes):
        code,value=self.request('/api/agent/drafts',self.body(**changes),role='agent')
        self.assertEqual(code,202,value)
        return value['draft']

    def approve(self,draft,**changes):
        return self.request('/api/owner/drafts/'+draft['id']+'/approve',{
            'public_safe_approved':True,'review_digest':draft['review_digest'],
            'tool_review_hash':draft['tool_review_hash'],**changes})

    def test_agent_cannot_open_public_address_or_self_approve_with_a_flag(self):
        with patch.object(self.runtime.public,'start') as start:
            draft=self.draft(public=True)
            self.assertEqual(draft['state'],'pending')
            self.assertNotIn('url',draft)
            self.assertEqual(self.runtime.manager.list_shares(),[])
            self.assertEqual(self.runtime.jobs.usage()['used'],0)
            self.assertEqual(self.request('/api/agent/drafts',self.body(public_safe_approved=True),role='agent')[0],400)
            for path in ('/api/owner/shares','/api/owner/public/start','/api/owner/provider',
                         '/api/owner/drafts/'+draft['id']+'/approve'):
                self.assertEqual(self.request(path,{'public_safe_approved':True},role='agent')[0],401)
            self.assertEqual(self.request('/api/agent/drafts/'+draft['id']+'/approve',{},role='agent')[0],403)
            start.assert_not_called()

    def test_agent_read_access_is_limited_and_guest_listener_rejects_both_roles(self):
        code,created=self.request('/api/owner/shares',self.body(public_safe_approved=True))
        self.assertEqual(code,201,created)
        self.assertEqual(self.request('/api/agent/state',role='agent')[1]['shares'],[])
        self.assertEqual(self.request('/api/agent/shares/'+created['id']+'/revoke',{},role='agent')[0],403)
        self.assertEqual(self.request('/api/owner/state',role='agent')[0],401)
        self.assertNotEqual(self.runtime.manager.agent_token,self.runtime.manager.owner_token)
        for role in ('agent','owner'):
            self.assertEqual(self.request('/api/agent/state',role=role,guest=True)[0],401)

    def test_owner_approves_immutable_draft_once_and_expiry_starts_at_approval(self):
        draft=self.draft()
        self.assertEqual(self.request('/api/owner/drafts/'+draft['id']+'/approve',
            {'review_digest':draft['review_digest'],'public_safe_approved':True,'max_calls':100})[0],400)
        self.assertEqual(self.approve(draft,review_digest='0'*64)[0],409)
        code,first=self.approve(draft);self.assertEqual(code,200,first)
        self.assertEqual(self.approve(draft)[1]['id'],first['id'])
        self.assertEqual(len(self.runtime.manager.list_shares()),1)
        self.assertEqual(self.runtime.manager.list_shares()[0]['max_calls'],2)
        self.assertEqual(self.request('/api/agent/state',role='agent')[1]['shares'][0]['id'],first['id'])
        self.assertEqual(self.request('/api/agent/shares/'+first['id']+'/revoke',{},role='agent')[0],200)
        self.approve(draft)
        self.assertEqual(self.runtime.manager.list_shares()[0]['status'],'revoked')

    def test_public_draft_stays_pending_until_owner_has_opened_a_public_address(self):
        draft=self.draft(public=True)
        self.assertEqual(self.approve(draft)[0],409)
        self.assertEqual(self.runtime.manager.list_shares(),[])
        self.runtime.public._info={'state':'live','origin':'https://unit.trycloudflare.com','error':None}
        code,value=self.approve(draft)
        self.assertEqual(code,200,value)
        self.assertTrue(value['url'].startswith('https://unit.trycloudflare.com/'))

    def test_rejected_and_expired_requests_cannot_be_approved(self):
        draft=self.draft()
        self.assertEqual(self.request('/api/owner/drafts/'+draft['id']+'/reject',{})[0],200)
        self.assertEqual(self.approve(draft)[0],409)
        draft=self.draft()
        self.runtime.manager._clock=lambda:draft['expires_at']+1
        self.assertEqual(self.approve(draft)[0],409)

    def test_tool_change_invalidates_pending_review(self):
        draft=self.draft()
        self.entries[0]['description']='Different behaviour and account'
        code,value=self.approve(draft)
        self.assertEqual((code,value['code']),(409,'tool_changed'))
        self.assertEqual(self.runtime.manager.list_shares(),[])

    def test_pending_request_survives_service_restart(self):
        draft=self.draft()
        old_owner=self.runtime.manager.owner_token
        self.runtime.close();self.runtime.jobs.executor.shutdown(wait=True,cancel_futures=True)
        self.runtime=Runtime(self.temp.name,direct_dispatch,lambda:self.entries,connection={'mode':'test'},port=0,guest_port=0).start()
        self.base='http://127.0.0.1:'+str(self.runtime.info['owner_port'])
        self.guest='http://127.0.0.1:'+str(self.runtime.info['guest_port'])
        self.assertEqual(self.runtime.manager.owner_token,old_owner)
        pending=self.request('/api/agent/state',role='agent')[1]['drafts']
        self.assertEqual(pending[0]['review_digest'],draft['review_digest'])
        self.assertEqual(self.approve(draft)[0],200)

    def test_concurrent_duplicate_owner_approval_creates_one_share(self):
        draft=self.draft()
        with ThreadPoolExecutor(max_workers=6) as workers:
            results=list(workers.map(lambda _:self.approve(draft),range(6)))
        self.assertTrue(all(code==200 for code,_ in results),results)
        self.assertEqual(len({value['id'] for _,value in results}),1)
        self.assertEqual(len(self.runtime.manager.list_shares()),1)

    def test_legacy_owner_key_is_never_accepted_after_upgrade(self):
        # A running 0.4 controller rereads this path. Even replacing it with a
        # known value cannot confer the new creator role.
        legacy=secrets.token_urlsafe(32)
        Path(self.temp.name,'owner.key').write_text(legacy,encoding='utf-8')
        self.runtime.close();self.runtime.jobs.executor.shutdown(wait=True,cancel_futures=True)
        self.runtime=Runtime(self.temp.name,direct_dispatch,lambda:self.entries,connection={'mode':'test'},port=0,guest_port=0).start()
        self.base='http://127.0.0.1:'+str(self.runtime.info['owner_port'])
        self.guest='http://127.0.0.1:'+str(self.runtime.info['guest_port'])
        self.assertNotEqual(self.runtime.manager.owner_token,legacy)
        for path in ('/api/owner/public/start','/api/owner/shares'):
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(self.base+path,data=json.dumps(self.body(public_safe_approved=True)).encode(),
                    headers={'Content-Type':'application/json','Authorization':'Bearer '+legacy}),timeout=5)
            self.assertEqual(error.exception.code,401)
        self.assertEqual(self.runtime.manager.list_shares(),[])

    def test_unknown_and_write_tools_require_exact_additional_owner_confirmation(self):
        schema=basic_bindings()[0]['schema']
        for annotations in (None,{'readOnlyHint':False},{'readOnlyHint':False,'destructiveHint':False}):
            self.entries=[tool_binding('fixture','change',schema,annotations=annotations)]
            body=self.body(fixed_args={},public_safe_approved=True)
            self.assertEqual(self.request('/api/owner/shares',body)[0],403)
            draft=self.draft(fixed_args={})
            self.assertTrue(draft['requires_side_effects_approval'])
            self.assertEqual(self.approve(draft)[0],403)
            self.assertEqual(self.approve(draft,allow_side_effects=True,tool_review_hash='bad')[0],409)
            self.assertEqual(self.approve(draft,allow_side_effects=True)[0],200)

    def test_explicit_destructive_hint_cannot_be_overridden_even_by_owner(self):
        self.entries=[tool_binding('fixture','erase',basic_bindings()[0]['schema'],annotations={'destructiveHint':True})]
        code,_=self.request('/api/owner/shares',self.body(fixed_args={},public_safe_approved=True,allow_side_effects=True))
        self.assertEqual(code,409)
        self.assertEqual(self.request('/api/agent/drafts',self.body(fixed_args={}),role='agent')[0],409)
        self.assertEqual(self.calls,[])

    def test_changed_risk_and_legacy_unreviewed_tools_fail_before_charging(self):
        self.entries=[tool_binding('fixture','write',basic_bindings()[0]['schema'])]
        token=self.runtime.manager.create(title='Legacy share',server='fixture',tool='write',
            schema=self.entries[0]['schema'],public_safe_approved=True,public_enabled=False)
        code,value=self.request('/api/s/'+token+'/jobs',{'input':{'text':'hello'},'request_id':secrets.token_hex(16)})
        self.assertEqual((code,value['code']),(403,'tool_approval_required'))
        draft=self.draft(fixed_args={})
        code,created=self.approve(draft,allow_side_effects=True);self.assertEqual(code,200,created)
        self.entries[0]['description']='Changed after review'
        token=created['url'].split('/s/')[1]
        self.assertEqual(self.request('/api/s/'+token+'/jobs',{'input':{'text':'hello'},'request_id':secrets.token_hex(16)})[0],403)
        self.assertEqual(self.runtime.jobs.usage()['used'],0)
        self.assertEqual(self.calls,[])

    def test_native_destructive_metadata_is_preserved_and_unknown_is_not_readonly(self):
        class Context:
            plugin_id='sharelet'
            def get_config(self,key,default=None):return default
        config={'plugins':{'entries':{'sharelet':{'mcp_allowlist':['files']}}},'mcp_servers':{'files':{}}}
        rows=[{'name':'mcp__files__erase','toolset':'mcp-files','schema':{'parameters':basic_bindings()[0]['schema'],
                'annotations':{'destructiveHint':True}}},
              {'name':'mcp__files__unknown','toolset':'mcp-files','schema':{'parameters':basic_bindings()[0]['schema']}}]
        entries=HermesBridge(Context(),inventory=lambda:rows,configuration=lambda:config).catalog()
        self.assertFalse(next(row for row in entries if row['tool'].endswith('erase'))['supported'])
        self.assertTrue(next(row for row in entries if row['tool'].endswith('unknown'))['requires_side_effects_approval'])


class LocalIdentityTests(unittest.TestCase):
    def test_wrong_listener_cannot_relay_another_ports_identity_proof(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime=Runtime(folder,direct_dispatch,basic_bindings,connection={'mode':'test'},port=0,guest_port=0).start()
            base='http://127.0.0.1:'+str(runtime.info['owner_port']);observed=[]
            class Handler(BaseHTTPRequestHandler):
                def log_message(self,*args):pass
                def do_GET(self):
                    observed.append(self.headers.get('Authorization'))
                    with urlopen(base+self.path,timeout=3) as response:body=response.read()
                    self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
            worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
            try:
                Path(folder,'service.json').write_text(json.dumps({'owner_port':server.server_port}))
                with self.assertRaises(CapsuleError):OwnerClient(folder,auto_start=False).request('/api/owner/state')
                self.assertEqual(observed,[None])
            finally:
                server.shutdown();server.server_close();worker.join(2)
                runtime.close();runtime.jobs.executor.shutdown(wait=True,cancel_futures=True)

    def test_wrong_listener_receives_no_owner_or_agent_credential(self):
        for role in ('owner','agent'):
            with tempfile.TemporaryDirectory() as folder:
                observed=[]
                class Handler(BaseHTTPRequestHandler):
                    def log_message(self,*args):pass
                    def do_GET(self):
                        observed.append(self.headers.get('Authorization'))
                        body=json.dumps({'ok':True,'profile_id':str(Path(folder).resolve()),'proof':'0'*64}).encode()
                        self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
                server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
                worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
                try:
                    Path(folder,'service.json').write_text(json.dumps({'owner_port':server.server_port}))
                    Path(folder,'owner-v2.key' if role=='owner' else 'agent.key').write_text(secrets.token_urlsafe(32))
                    with self.assertRaises(CapsuleError):
                        OwnerClient(folder,auto_start=False,role=role).request('/api/'+role+'/state')
                    self.assertEqual(observed,[None])
                finally:server.shutdown();server.server_close();worker.join(2)


if __name__=='__main__':unittest.main(verbosity=2)
