"""Real PDF processing and HTTP file capability boundaries (optional pypdf)."""
import io
import http.client
import json
import os
from pathlib import Path
import secrets
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request,urlopen

try:
    from pypdf import PdfReader,PdfWriter
    from examples.pdf_workshop.server import process_pdf,mcp
    PDF_AVAILABLE=True
except ImportError:
    PDF_AVAILABLE=False

from catalog import tool_binding
from file_inputs import FileStore,MAX_FILE
from service import Runtime
from sharelet import CapsuleError,normalise_schema


@unittest.skipUnless(PDF_AVAILABLE,'Install examples/pdf_workshop/requirements.txt for real PDF tests')
class FileHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.environment=patch.dict(os.environ,{'SHARELET_FILE_WORKSPACE':str(self.root)})
        self.environment.start()
        import asyncio
        tool=asyncio.run(mcp.list_tools())[0]
        self.entry=tool_binding('pdf_workshop',tool.name,tool.inputSchema,description=tool.description,
            annotations=tool.annotations.model_dump())
        self.calls=[]
        def dispatch(server,tool,args):
            self.calls.append(args)
            return {'ok':True,'result':process_pdf(**args)}
        self.runtime=Runtime(self.root,dispatch,lambda:[self.entry],connection={'mode':'test'},port=0,guest_port=0).start()
        # Controlled address state for HTTP boundary tests. Public deployment and
        # actual MCP transport are checked separately in the browser/integration run.
        self.runtime.public._info={'state':'live','origin':'https://file-tests.example','error':None}
        self.owner='http://127.0.0.1:'+str(self.runtime.info['owner_port'])
        self.base='http://127.0.0.1:'+str(self.runtime.info['guest_port'])

    def tearDown(self):
        self.runtime.close();self.runtime.jobs.executor.shutdown(wait=True,cancel_futures=True)
        self.environment.stop();self.temp.cleanup()

    @staticmethod
    def pdf(widths=(301,302),encrypted=False):
        writer=PdfWriter()
        for width in widths:writer.add_blank_page(width=width,height=400)
        if encrypted:writer.encrypt('test-password')
        stream=io.BytesIO();writer.write(stream);return stream.getvalue()

    def request(self,path,body=None,*,owner=False,raw=None,headers=None):
        head={'Content-Type':'application/json',**(headers or {})}
        if owner:head['Authorization']='Bearer '+self.runtime.manager.owner_token
        request=Request((self.owner if owner else self.base)+path,data=raw if raw is not None else None if body is None else json.dumps(body).encode(),headers=head)
        try:response=urlopen(request,timeout=10)
        except HTTPError as error:response=error
        with response:
            value=response.read()
            return response.code,json.loads(value) if response.headers.get_content_type()=='application/json' else value,response.headers

    def share(self,**changes):
        args={'tool_id':self.entry['id'],'title':'PDF workshop','public':True,'public_safe_approved':True,
              'allow_side_effects':True,'tool_review_hash':self.entry['review_hash'],'max_calls':10,'expires_seconds':3600}
        args.update(changes)
        status,value,_=self.request('/api/owner/shares',args,owner=True)
        self.assertEqual(status,201,value)
        return value['url'].split('/s/')[1]

    def upload(self,slug,raw=None,name='my file.pdf'):
        status,value,_=self.request('/api/s/'+slug+'/uploads',raw=raw or self.pdf(),headers={'Content-Type':'application/pdf','X-Sharelet-Filename':name})
        self.assertEqual(status,201,value);return value['upload']['id']

    def start(self,slug,files,action='Merge PDFs',pages='',key=None):
        body={'input':{'files':json.dumps(files),'action':action,'pages':pages},'request_id':key or secrets.token_hex(16)}
        return self.request('/api/s/'+slug+'/jobs',body),body

    def finish(self,slug,job):
        for _ in range(150):
            _,value,_=self.request('/api/s/'+slug+'/jobs/'+job['id']);job=value['job']
            if job['state'] not in ('queued','running'):return job
            time.sleep(.02)
        self.fail('PDF subprocess did not finish')

    def assert_inputs_removed(self):
        # The worker's finally block follows the durable terminal-state commit.
        # Check the observable cleanup, without racing that short transition.
        for _ in range(100):
            if not list(self.runtime.jobs.files.inbox.glob('*.pdf')):return
            time.sleep(.01)
        self.fail('Uploaded copies were retained after processing')

    def test_real_merge_reorder_download_refresh_and_input_cleanup(self):
        slug=self.share();a=self.upload(slug,self.pdf((301,302)));b=self.upload(slug,self.pdf((401,)))
        (status,value,_),body=self.start(slug,[b,a]);self.assertEqual(status,202,value)
        job=self.finish(slug,value['job']);self.assertEqual(job['state'],'succeeded',job)
        status,pdf,headers=self.request(job['file_url']);self.assertEqual(status,200)
        self.assertEqual([int(p.mediabox.width) for p in PdfReader(io.BytesIO(pdf)).pages],[401,301,302])
        self.assertEqual(headers.get_content_type(),'application/pdf');self.assertIn('attachment',headers['Content-Disposition'])
        self.assert_inputs_removed()
        self.assertNotIn('asset_id',job['result']);self.assertNotIn(str(self.root),json.dumps(job))
        status,replayed,_=self.request('/api/s/'+slug+'/jobs',body)
        self.assertEqual(status,202);self.assertEqual(replayed['job']['id'],job['id']);self.assertEqual(len(self.calls),1)
        self.assertEqual(self.runtime.jobs.budget()['used'],0)

    def test_real_page_extraction_and_compression(self):
        slug=self.share();a=self.upload(slug,self.pdf((301,302,303)))
        (_,value,_),_=self.start(slug,[a],'Extract pages','3,1-2')
        job=self.finish(slug,value['job']);self.assertEqual(job['state'],'succeeded',job)
        _,raw,_=self.request(job['file_url'])
        self.assertEqual([int(p.mediabox.width) for p in PdfReader(io.BytesIO(raw)).pages],[303,301,302])
        a=self.upload(slug,self.pdf((401,)))
        (_,value,_),_=self.start(slug,[a],'Lossless compression')
        job=self.finish(slug,value['job']);self.assertEqual(job['state'],'succeeded',job)
        _,raw,_=self.request(job['file_url']);self.assertEqual(len(PdfReader(io.BytesIO(raw)).pages),1)

    def test_cross_share_input_output_and_path_injection_rejected(self):
        first,second=self.share(),self.share();a=self.upload(first)
        before=self.runtime.jobs.usage()['used']
        (status,value,_),_=self.start(second,[a],'Extract pages','1')
        self.assertEqual(status,400,value);self.assertEqual(self.runtime.jobs.usage()['used'],before)
        (status,value,_),_=self.start(first,['../../secrets'],'Extract pages','1')
        self.assertEqual(status,400,value);self.assertEqual(len(self.calls),0)
        (_,value,_),_=self.start(first,[a],'Extract pages','1');job=self.finish(first,value['job'])
        self.assertEqual(self.request('/api/s/'+second+'/jobs/'+job['id']+'/file')[0],404)
        self.runtime.manager.revoke(first)
        self.assertEqual(self.request(job['file_url'])[0],410)

    def test_invalid_uploads_and_quota_do_not_consume_runs(self):
        slug=self.share()
        self.assertEqual(self.request('/api/s/'+slug+'/uploads',raw=b'not a pdf',headers={'Content-Type':'application/pdf'})[0],400)
        # Check early rejection from framing alone; sending 5 MB after the
        # listener has already replied can yield a platform-specific broken pipe.
        connection=http.client.HTTPConnection('127.0.0.1',self.runtime.info['guest_port'],timeout=10)
        try:
            connection.putrequest('POST','/api/s/'+slug+'/uploads')
            connection.putheader('Content-Type','application/pdf');connection.putheader('Content-Length',str(MAX_FILE+1))
            connection.endheaders();self.assertEqual(connection.getresponse().status,413)
        finally:connection.close()
        ids=[self.upload(slug) for _ in range(8)]
        self.assertEqual(self.request('/api/s/'+slug+'/uploads',raw=self.pdf(),headers={'Content-Type':'application/pdf'})[0],413)
        self.assertEqual(self.request('/api/s/'+slug+'/uploads/'+ids[0]+'/remove',{})[0],200)
        self.upload(slug)
        self.assertEqual(self.runtime.jobs.usage()['used'],0)

    def test_pdf_errors_are_specific_and_uploaded_copies_are_removed(self):
        slug=self.share()
        for raw,action,pages,fragment in [(self.pdf(encrypted=True),'Extract pages','1','Password-protected'),
            (self.pdf(),'Extract pages','99','outside'),(b'%PDF-1.4\ninvalid\n%%EOF\n','Extract pages','1','readable PDF')]:
            a=self.upload(slug,raw)
            (_,value,_),_=self.start(slug,[a],action,pages);job=self.finish(slug,value['job'])
            self.assertEqual(job['state'],'failed');self.assertIn(fragment,job['error'])
            self.assert_inputs_removed()

    def test_expired_upload_cannot_be_admitted(self):
        slug=self.share();a=self.upload(slug)
        with self.runtime.manager._db() as db:db.execute('UPDATE file_uploads SET expires_at=0 WHERE id=?',(a,))
        (status,value,_),_=self.start(slug,[a],'Extract pages','1')
        self.assertEqual(status,400,value);self.assertEqual(len(self.calls),0)
        self.runtime.jobs.files.cleanup();self.assertFalse(list(self.runtime.jobs.files.inbox.glob('*.pdf')))

    def test_file_paths_in_receipts_and_fixed_uploads_are_rejected(self):
        receipt={'kind':'file','asset_id':'../../private','content_type':'application/pdf',
                 'filename':'result.pdf','pages':1,'input_bytes':100}
        with self.assertRaises(CapsuleError):self.runtime.jobs.files.save('a'*32,receipt)
        if os.name!='nt':
            private=self.root/'private.pdf';private.write_bytes(self.pdf())
            (self.runtime.jobs.files.outbox/('b'*32+'.pdf')).symlink_to(private)
            with self.assertRaises(CapsuleError):self.runtime.jobs.files.save('a'*32,dict(receipt,asset_id='b'*32))
        status,value,_=self.request('/api/owner/shares',{'tool_id':self.entry['id'],'title':'Invalid','public':False,
            'fixed_args':{'files':'["'+'a'*32+'"]'},'public_safe_approved':True},owner=True)
        self.assertEqual(status,400,value)
        with self.assertRaises(CapsuleError):normalise_schema({'type':'object','properties':{'f':{'type':'string','format':'arbitrary-file-path'}}})
