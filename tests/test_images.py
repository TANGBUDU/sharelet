"""Boundary tests use controlled providers; native/provider checks are separate."""
import ast
import base64
import concurrent.futures
import contextvars
import json
from pathlib import Path
import secrets
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zlib
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request,urlopen

from host import HermesBridge
from image_provider import ProviderSettings,generate_avatar
from jobs import ImageJobs
from media import MediaStore,validate_png
from presets import image_bindings,MODEL,SIZE
from service import Runtime,ServiceLease
from sharelet import CapsuleError,CapsuleManager
from webserver import CapabilityLimiter,create_server

def png_chunk(kind, data):
    return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)

PNG=(b'\x89PNG\r\n\x1a\n'+png_chunk(b'IHDR',struct.pack('>IIBBBBB',SIZE,SIZE,8,2,0,0,0))
     +png_chunk(b'IDAT',zlib.compress((b'\x00'+b'\x88\xaa\x99'*SIZE)*SIZE))+png_chunk(b'IEND',b''))
ROOT=Path(__file__).resolve().parents[1]


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.calls=[]
        def dispatch(server,tool,args):
            self.calls.append((server,tool,args))
            asset=secrets.token_hex(16)
            outbox=Path(self.temp.name)/'provider-outbox';outbox.mkdir(exist_ok=True)
            (outbox/(asset+'.png')).write_bytes(PNG)
            return {'ok':True,'result':{'kind':'image','asset_id':asset}}
        self.m=CapsuleManager(self.temp.name,dispatch)
        self.media=MediaStore(self.temp.name)
        self.jobs=ImageJobs(self.m,self.media,limit=5)
        self.token=self.create()

    def tearDown(self):
        self.jobs.executor.shutdown(wait=True,cancel_futures=True)
        self.temp.cleanup()

    def create(self,**changes):
        recipe=image_bindings()[0]
        args=dict(title='Clay studio',description='An image recipe',server=recipe['server'],tool=recipe['tool'],schema=recipe['schema'],fixed_args=recipe['locked_args'],kind='image',preset='clay',max_calls=5,expires_seconds=3600,public_safe_approved=True)
        args.update(changes)
        return self.m.create(**args)

    def finish(self,token,job):
        for _ in range(150):
            current=self.jobs.get(token,job['id'])
            if current['state'] not in ('queued','running'):return current
            time.sleep(.01)
        self.fail('job did not finish')

    def test_one_real_admission_for_concurrent_repeated_request(self):
        key=secrets.token_hex(16)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda _:self.jobs.submit(self.token,{'subject':'a tiny fox'},key),range(8)))
        self.assertEqual(len({item['id'] for item in results}),1)
        final=self.finish(self.token,results[0]);self.assertEqual(final['state'],'succeeded')
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.m.public_info(self.token)['calls'],1)
        self.assertEqual(self.jobs.budget()['used'],1)

    def test_request_key_cannot_be_reused_for_changed_input(self):
        key=secrets.token_hex(16)
        job=self.jobs.submit(self.token,{'subject':'a tiny fox'},key)
        self.finish(self.token,job)
        with self.assertRaises(CapsuleError) as error:self.jobs.submit(self.token,{'subject':'a large cat'},key)
        self.assertEqual(error.exception.code,'request_conflict')
        self.assertEqual(len(self.calls),1)

    def test_model_size_count_and_style_are_not_guest_inputs(self):
        for forbidden in ({'model':'expensive-model'},{'size':8192},{'num_images':4},{'style':'anime'}):
            with self.assertRaises(CapsuleError):self.jobs.submit(self.token,{'subject':'a tiny fox',**forbidden},secrets.token_hex(16))
        self.assertEqual(self.calls,[])
        self.assertEqual(self.jobs.budget()['used'],0)

    def test_dispatched_recipe_has_fixed_parameters(self):
        job=self.jobs.submit(self.token,{'subject':'a tiny fox'},secrets.token_hex(16))
        self.assertEqual(self.finish(self.token,job)['state'],'succeeded')
        args=self.calls[0][2]
        self.assertEqual((args['model'],args['size'],args['num_images'],args['style']),(MODEL,SIZE,1,'clay'))

    def test_missing_provider_is_rejected_before_charging(self):
        self.jobs.require_ready=ProviderSettings(self.temp.name).require_ready
        with patch.dict('os.environ',{},clear=True):
            with self.assertRaises(CapsuleError) as error:self.jobs.submit(self.token,{'subject':'a tiny fox'},secrets.token_hex(16))
        self.assertEqual(error.exception.code,'provider_not_configured')
        self.assertEqual(self.jobs.budget()['used'],0)
        self.assertEqual(self.m.public_info(self.token)['calls'],0)

    def test_polling_and_recovery_never_resubmit_a_job(self):
        key=secrets.token_hex(16)
        job=self.jobs.submit(self.token,{'subject':'a tiny fox'},key)
        self.finish(self.token,job)
        for _ in range(8):
            self.jobs.get(self.token,job['id']);self.jobs.by_request(self.token,key)
        self.jobs.submit(self.token,{'subject':'a tiny fox'},key)
        self.assertEqual(len(self.calls),1)

    def test_restart_does_not_replay_an_uncertain_request(self):
        key=secrets.token_hex(16)
        with patch.object(self.jobs.executor,'submit'):
            job=self.jobs.submit(self.token,{'subject':'a tiny fox'},key)
        self.jobs.recover_interrupted()
        restored=self.jobs.submit(self.token,{'subject':'a tiny fox'},key)
        self.assertEqual(restored['id'],job['id'])
        self.assertEqual(restored['state'],'interrupted')
        self.assertEqual(self.calls,[])
        self.assertEqual(self.jobs.budget()['used'],1)

    def test_cross_link_host_budget_is_persistent(self):
        self.jobs.limit=1
        job=self.jobs.submit(self.token,{'subject':'a tiny fox'},secrets.token_hex(16));self.finish(self.token,job)
        another=self.create()
        with self.assertRaises(CapsuleError) as error:self.jobs.submit(another,{'subject':'a tiny fox'},secrets.token_hex(16))
        self.assertEqual(error.exception.code,'profile_budget')
        self.assertEqual(len(self.calls),1)

    def test_another_link_cannot_read_a_job_or_image(self):
        job=self.jobs.submit(self.token,{'subject':'a tiny fox'},secrets.token_hex(16));self.finish(self.token,job)
        another=self.create()
        with self.assertRaises(CapsuleError):self.jobs.get(another,job['id'])

    def test_last_image_can_be_downloaded_after_quota_is_used_up(self):
        token=self.create(max_calls=1)
        job=self.jobs.submit(token,{'subject':'a tiny fox'},secrets.token_hex(16));self.finish(token,job)
        self.assertEqual(self.m.public_info(token)['status'],'exhausted')
        self.assertEqual(self.jobs.get(token,job['id'])['state'],'succeeded')
        self.assertTrue(self.media.path(job['id']).exists())

    def test_revocation_denies_delivery_and_cleans_cached_media(self):
        job=self.jobs.submit(self.token,{'subject':'a tiny fox'},secrets.token_hex(16));self.finish(self.token,job)
        self.m.revoke(self.token)
        with self.assertRaises(CapsuleError):self.jobs.get(self.token,job['id'])
        self.jobs.cleanup_media()
        self.assertFalse(self.media.path(job['id']).exists())

    def test_provider_failure_is_not_retried_and_does_not_leak_details(self):
        def failed(*_):raise TimeoutError('private credential must not appear')
        self.m._call_tool=failed
        key=secrets.token_hex(16)
        job=self.jobs.submit(self.token,{'subject':'a tiny fox'},key)
        result=self.finish(self.token,job)
        self.assertEqual(result['state'],'failed')
        self.assertNotIn('private credential',result['error'])
        self.assertEqual(self.jobs.submit(self.token,{'subject':'a tiny fox'},key)['id'],job['id'])
        self.assertEqual(self.jobs.budget()['used'],1)


class RuntimeTests(unittest.TestCase):
    def test_same_proxy_ip_does_not_combine_separate_links(self):
        limiter=CapabilityLimiter(per_link=2,global_limit=20)
        self.assertTrue(limiter.admit('link-a'));self.assertTrue(limiter.admit('link-a'))
        self.assertFalse(limiter.admit('link-a'))
        self.assertTrue(limiter.admit('link-b'))

    def test_limiter_has_an_explicit_service_ceiling(self):
        limiter=CapabilityLimiter(per_link=2,global_limit=2)
        limiter.admit('a');limiter.admit('b')
        self.assertFalse(limiter.admit('c'))

    def test_os_lease_excludes_another_process(self):
        with tempfile.TemporaryDirectory() as folder:
            lease=ServiceLease(folder);self.assertTrue(lease.acquire())
            code='from service import ServiceLease; import sys; lock=ServiceLease(sys.argv[1]); print(lock.acquire())'
            result=subprocess.run([sys.executable,'-c',code,folder],cwd=ROOT,capture_output=True,text=True,check=True)
            self.assertEqual(result.stdout.strip(),'False')
            lease.release()
            result=subprocess.run([sys.executable,'-c',code,folder],cwd=ROOT,capture_output=True,text=True,check=True)
            self.assertEqual(result.stdout.strip(),'True')

    def test_occupied_ports_fall_back_without_hijacking_the_listener(self):
        with tempfile.TemporaryDirectory() as folder:
            m=CapsuleManager(folder,lambda *_:{'ok':True,'result':None})
            occupied=create_server(m,lambda:[])
            fallback=create_server(m,lambda:[],port=occupied.server_port,auto_port=True)
            try:self.assertNotEqual(fallback.server_port,occupied.server_port)
            finally:fallback.server_close();occupied.server_close()

    def test_second_runtime_attaches_and_takes_over_after_owner_stops(self):
        with tempfile.TemporaryDirectory() as folder:
            dispatch=lambda *_:{'ok':True,'result':None}
            first=Runtime(folder,dispatch,lambda:[],connection={'mode':'test'},port=0,guest_port=0).start()
            second=Runtime(folder,dispatch,lambda:[],connection={'mode':'test'},port=0,guest_port=0).start()
            try:
                self.assertEqual(second.info['service'],'attached')
                self.assertEqual(second.info['owner_port'],first.info['owner_port'])
                first.close()
                for _ in range(60):
                    if second.info['service']=='listening':break
                    time.sleep(.05)
                self.assertEqual(second.info['service'],'listening')
            finally:second.close();first.close()

    def test_public_sdk_bridge_keeps_the_owners_context(self):
        marker=contextvars.ContextVar('test_owner')
        class Context:
            def call_mcp(self,*args,**kwargs):return marker.get()
            def get_config(self,key,default=None):return default
        token=marker.set('owner-a')
        bridge=HermesBridge(Context());marker.reset(token)
        token=marker.set('owner-b')
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                self.assertEqual(list(pool.map(lambda _:bridge.call('s','t',{}),range(4))),['owner-a']*4)
        finally:marker.reset(token)

    def test_runtime_has_no_private_hermes_imports(self):
        for filename in ('__init__.py','host.py','service.py','webserver.py','jobs.py','tunnel.py'):
            tree=ast.parse((ROOT/filename).read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node,ast.ImportFrom):
                    module=node.module or ''
                    if module in ('hermes_cli.config','tools.registry'):
                        self.assertFalse(any(name.name.startswith('_') for name in node.names),filename)
                    else:
                        self.assertFalse(module.startswith(('hermes_cli','hermes_constants','tools.')),filename)

    def test_provider_key_is_never_in_public_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            settings=ProviderSettings(folder);key=secrets.token_urlsafe(32)
            response=settings.save({'openai_key':key})
            self.assertTrue(response['configured']);self.assertNotIn(key,json.dumps(response))
            self.assertNotIn(key,json.dumps(settings.public()))

    def test_media_receipt_cannot_supply_a_url_or_arbitrary_path(self):
        with tempfile.TemporaryDirectory() as folder:
            media=MediaStore(folder)
            for response in ({'kind':'image','url':'http://localhost/secret'},
                             {'kind':'image','asset_id':'../../secret'},
                             {'kind':'image','asset_id':'/etc/passwd'}):
                with self.assertRaises(CapsuleError):media.save(secrets.token_hex(16),response)

    def test_truncated_or_changed_png_is_rejected(self):
        validate_png(PNG)
        for data in (PNG[:-12],PNG[:40]+b'x'+PNG[41:],b'<svg>not an image</svg>'):
            with self.assertRaises(CapsuleError):validate_png(data)

    def test_provider_adapter_submits_once_with_fixed_billing_parameters(self):
        with tempfile.TemporaryDirectory() as folder:
            settings=ProviderSettings(folder);settings.save({'openai_key':secrets.token_urlsafe(32)})
            result={'data':[{'b64_json':base64.b64encode(PNG).decode()}]}
            with patch('image_provider._openai_json',return_value=result) as transport:
                out=generate_avatar('a tiny fox','clay',settings_path=settings.path)
            self.assertEqual(out['kind'],'image')
            transport.assert_called_once()
            payload=transport.call_args.args[1]
            self.assertEqual((payload['n'],payload['size'],payload['model'],payload['quality']),(1,f'{SIZE}x{SIZE}',MODEL,'low'))
            self.assertEqual(payload['moderation'],'auto')
            self.assertNotIn('b64_json',json.dumps(out))
            media=MediaStore(folder);media.save('a'*32,out)
            self.assertEqual(media.path('a'*32).read_bytes(),PNG)
            self.assertEqual(list((Path(folder)/'provider-outbox').glob('*.png')),[])

    def test_other_credential_files_are_never_read(self):
        with tempfile.TemporaryDirectory() as folder,patch.dict('os.environ',{},clear=True):
            from image_provider import atomic_private
            key=secrets.token_urlsafe(32)
            existing=Path(folder)/'existing.env';existing.write_text('OPENAI_API_KEY='+key)
            settings=ProviderSettings(folder)
            atomic_private(settings.path,{'credential_file':str(existing)})
            self.assertFalse(settings.public()['configured'])

    def test_provider_timeout_is_never_resubmitted(self):
        with tempfile.TemporaryDirectory() as folder:
            settings=ProviderSettings(folder);settings.save({'openai_key':secrets.token_urlsafe(32)})
            with patch('image_provider._openai_json',side_effect=TimeoutError('secret detail')) as transport:
                with self.assertRaisesRegex(RuntimeError,'no automatic resubmission'):
                    generate_avatar('a tiny fox','clay',settings_path=settings.path)
            transport.assert_called_once()
            self.assertFalse((Path(folder)/'provider-outbox').exists())


if __name__=='__main__':unittest.main(verbosity=2)
