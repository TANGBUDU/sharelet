"""Independent review regressions: limiter poisoning, liveness, nulls and billing consent."""
import base64
import json
from pathlib import Path
import secrets
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request,urlopen

from catalog import tool_binding
from host import HermesBridge
from image_provider import ProviderSettings,generate_avatar
from jobs import ImageJobs
from media import MediaStore
from public_tools import basic_bindings,direct_dispatch
from service import Runtime
from sharelet import CapsuleError,CapsuleManager,normalise_schema,validate_values
from test_images import PNG
from tunnel import PublicLink


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.calls=[];self.gate=threading.Event()
        def call(server,tool,args):
            self.calls.append(args)
            if args['text'].startswith('slow'):self.gate.wait(5)
            return direct_dispatch(server,tool,args)
        self.runtime=Runtime(self.temp.name,call,basic_bindings,connection={'mode':'test'},port=0,guest_port=0).start()
        self.base='http://127.0.0.1:'+str(self.runtime.info['owner_port'])
        self.owner=self.runtime.manager.owner_token

    def tearDown(self):
        self.gate.set();self.runtime.close();self.runtime.jobs.executor.shutdown(wait=True,cancel_futures=True);self.temp.cleanup()

    def request(self,path,body=None,raw=None,agent=False):
        key=self.runtime.manager.agent_token if agent else self.owner
        try:r=urlopen(Request(self.base+path,data=raw if raw is not None else json.dumps(body).encode() if body is not None else None,
            headers={'Content-Type':'application/json','Authorization':'Bearer '+key}),timeout=5)
        except HTTPError as error:r=error
        with r:return r.code,json.load(r)

    def share(self):
        code,value=self.request('/api/owner/shares',{'tool_id':'sharelet_public::text_cleanup','title':'Review probe',
            'public_safe_approved':True,'public':False,'max_calls':50})
        self.assertEqual(code,201,value);return value['url'].split('/s/')[1]

    def submit(self,slug,text,request_id=None):
        return self.request('/api/s/'+slug+'/jobs',{'input':{'text':text},'request_id':request_id or secrets.token_hex(16)})

    def finish(self,slug,job):
        for _ in range(200):
            value=self.request('/api/s/'+slug+'/jobs/'+job['id'])[1]['job']
            if value['state'] not in ('queued','running'):return value
            time.sleep(.01)
        self.fail('Controlled job did not finish')

    def test_invalid_bodies_and_duplicate_recovery_do_not_poison_shared_limiter(self):
        slug=self.share()
        for _ in range(12):
            self.assertEqual(self.request('/api/s/'+slug+'/jobs',raw=b'not json')[0],400)
            self.assertEqual(self.submit(slug,False)[0],400)
        request_id=secrets.token_hex(16)
        code,value=self.submit(slug,'hello',request_id);self.assertEqual(code,202,value)
        job=self.finish(slug,value['job'])
        for _ in range(12):
            code,value=self.submit(slug,'hello',request_id)
            self.assertEqual(code,202,value);self.assertEqual(value['job']['id'],job['id'])
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.runtime.jobs.usage()['used'],1)

    def test_two_slow_links_do_not_immediately_reject_an_unrelated_link(self):
        shares=[self.share() for _ in range(3)]
        self.assertEqual(self.submit(shares[0],'slow one')[0],202)
        self.assertEqual(self.submit(shares[1],'slow two')[0],202)
        code,value=self.submit(shares[2],'fast third');self.assertEqual(code,202,value)
        self.assertEqual(self.finish(shares[2],value['job'])['state'],'succeeded')
        self.assertFalse(self.gate.is_set())

    def test_owner_can_change_persisted_limits_without_erasing_spent_attempts(self):
        slug=self.share();_,value=self.submit(slug,'one');self.finish(slug,value['job'])
        self.assertEqual(self.request('/api/owner/limits',{'execution_limit':7,'generation_limit':0},agent=True)[0],401)
        code,value=self.request('/api/owner/limits',{'execution_limit':7,'generation_limit':0})
        self.assertEqual(code,200,value);self.assertEqual(value['usage']['used'],1)
        other=ImageJobs(self.runtime.manager,MediaStore(self.temp.name))
        try:self.assertEqual((other.usage()['limit'],other.usage()['used'],other.budget()['limit']),(7,1,0))
        finally:other.close();other.executor.shutdown(wait=True)
        self.assertEqual(self.request('/api/owner/limits',{'execution_limit':0,'generation_limit':0})[0],400)


class QueuedCallTests(unittest.TestCase):
    def test_queue_is_bounded_and_revoked_waiting_job_never_calls_tool(self):
        with tempfile.TemporaryDirectory() as folder:
            gate=threading.Event();entered=threading.Event();calls=[]
            def invoke(server,tool,args):
                calls.append(args);entered.set();gate.wait(5);return {'ok':True,'result':args}
            manager=CapsuleManager(folder,invoke)
            jobs=ImageJobs(manager,MediaStore(folder),workers=1,queue_limit=2)
            schema=basic_bindings()[0]['schema']
            def share():return manager.create(title='Queued fixture',server='fixture',tool='run',schema=schema,public_safe_approved=True)
            one,two,three=share(),share(),share()
            try:
                jobs.submit(one,{'text':'first'},secrets.token_hex(16));self.assertTrue(entered.wait(1))
                waiting=jobs.submit(two,{'text':'second'},secrets.token_hex(16))
                with self.assertRaises(CapsuleError):jobs.submit(three,{'text':'overflow'},secrets.token_hex(16))
                manager.revoke(two);gate.set();jobs.executor.shutdown(wait=True)
                self.assertEqual(len(calls),1)
                with manager._db() as db:
                    self.assertEqual(db.execute('SELECT state FROM image_jobs WHERE id=?',(waiting['id'],)).fetchone()[0],'interrupted')
                # The revoked waiting job was never sent to the tool, so its attempt is refunded.
                self.assertEqual(jobs.usage()['used'],1)
            finally:gate.set();jobs.close();jobs.executor.shutdown(wait=True)


class NativeCallTests(unittest.TestCase):
    def bridge(self,handler,timeout=180):
        class Context:
            plugin_id='sharelet'
            def dispatch_tool(self,name,args):return handler(name,args)
            def get_config(self,key,default=None):return default
        config={'plugins':{'entries':{'sharelet':{'mcp_allowlist':['fixture']}}},'mcp_servers':{'fixture':{}}}
        inventory=[{'name':'mcp__fixture__read','toolset':'mcp-fixture','schema':{'parameters':basic_bindings()[0]['schema']}}]
        return HermesBridge(Context(),inventory=lambda:inventory,configuration=lambda:config,call_timeout=timeout)

    def test_business_error_field_is_not_a_transport_failure(self):
        result=self.bridge(lambda *_:json.dumps({'error':'no matching records','count':0})).call('fixture','mcp__fixture__read',{})
        self.assertTrue(result['ok']);self.assertEqual(result['result']['count'],0)

    def test_native_deadline_marks_uncertain_without_repeating_the_call(self):
        gate=threading.Event();calls=[]
        def handler(*args):calls.append(args);gate.wait(2);return '{}'
        try:
            result=self.bridge(handler,.04).call('fixture','mcp__fixture__read',{})
            self.assertTrue(result['uncertain']);self.assertFalse(result['ok']);self.assertEqual(len(calls),1)
        finally:gate.set()


class NullableTests(unittest.TestCase):
    def test_optional_scalar_and_null_are_normalised_without_weakening_validation(self):
        for field in ({'anyOf':[{'type':'integer','minimum':0},{'type':'null'}],'default':None},
                      {'type':['integer','null'],'minimum':0,'default':None}):
            schema=normalise_schema({'type':'object','properties':{'limit':field},'required':['limit']})
            self.assertEqual(normalise_schema(schema),schema)
            self.assertTrue(validate_values(schema,{'limit':None}))
            self.assertTrue(validate_values(schema,{'limit':0}))
            for value in (False,'1',-1):
                with self.assertRaises(CapsuleError):validate_values(schema,{'limit':value})
            with self.assertRaises(CapsuleError):validate_values(schema,{})

    def test_nullable_does_not_admit_arbitrary_unions_or_silently_drop_rules(self):
        for field in ({'anyOf':[{'type':'integer'},{'type':'string'}]},
                      {'anyOf':[{'type':'array','items':{'type':'string'}},{'type':'null'}]},
                      {'anyOf':[{'type':'integer','minimum':0},{'type':'null'}],'minimum':-100}):
            with self.assertRaises(CapsuleError):normalise_schema({'type':'object','properties':{'limit':field}})


class ProviderConsentTests(unittest.TestCase):
    def test_host_environment_key_is_inactive_until_owner_explicitly_enables_it(self):
        key=secrets.token_urlsafe(32)
        with tempfile.TemporaryDirectory() as folder,patch.dict('os.environ',{'OPENAI_API_KEY':key}):
            settings=ProviderSettings(folder)
            self.assertTrue(settings.public()['host_key_available']);self.assertFalse(settings.public()['configured'])
            with patch('image_provider._openai_json') as transport:
                with self.assertRaises(RuntimeError):generate_avatar('test fox','clay',settings_path=settings.path)
                transport.assert_not_called()
            self.assertTrue(settings.save({'use_host_key':True})['configured'])
            self.assertNotIn(key,settings.path.read_text())
            response={'data':[{'b64_json':base64.b64encode(PNG).decode()}]}
            with patch('image_provider._openai_json',return_value=response) as transport:
                generate_avatar('test fox','clay',settings_path=settings.path);transport.assert_called_once()


class TunnelHealthTests(unittest.TestCase):
    def test_live_process_with_dead_public_address_is_detected(self):
        closed=threading.Event();checks=[]
        class Output:
            def __iter__(self):
                yield 'https://fixture.trycloudflare.com\n'
                closed.wait(5)
        class Process:
            stdout=Output()
            def poll(self):return 0 if closed.is_set() else None
            def terminate(self):closed.set()
            def wait(self,timeout=None):return 0
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self):return b'{"ok":true,"guest_only":true}'
        def request(*args,**kwargs):
            checks.append(1)
            if len(checks)>1:raise ConnectionError('controlled offline edge')
            return Response()
        with tempfile.TemporaryDirectory() as folder:
            link=PublicLink(folder);link.bin.parent.mkdir();link.bin.write_bytes(b'controlled');link.guest_port=12345
            with patch('tunnel.subprocess.Popen',return_value=Process()),patch('tunnel.urlopen',side_effect=request),patch('tunnel.HEALTH_INTERVAL',.02):
                try:
                    link.start()
                    for _ in range(150):
                        if link.status()['state']=='failed':break
                        time.sleep(.01)
                    self.assertEqual(link.status()['state'],'failed')
                    self.assertGreaterEqual(len(checks),3);self.assertTrue(closed.is_set())
                finally:link.stop()


if __name__=='__main__':unittest.main(verbosity=2)
