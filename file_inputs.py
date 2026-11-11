"""Capability-scoped PDF uploads and local file receipts for reviewed MCP tools.

The browser and tool exchange opaque IDs, never host paths. Input IDs belong to
one share and are locked to one admitted job. Outputs are served as attachments.
"""
import json
import os
from pathlib import Path
import re
import secrets
import unicodedata

try:
    from .sharelet import CapsuleError
    from .media import unwrap_result
except ImportError:
    from sharelet import CapsuleError
    from media import unwrap_result

PDF_FORMAT='sharelet-pdf-uploads'
MAX_FILE=5*1024*1024
MAX_INPUT=20*1024*1024
MAX_OUTPUT=25*1024*1024
MAX_STORAGE=256*1024*1024
UPLOAD_TTL=900
ID=re.compile(r'[a-f0-9]{32}')
FILE_ERRORS={
    'invalid_pdf':'One of the files is not a readable PDF. Choose a different file.',
    'encrypted_pdf':'Password-protected PDFs are not supported. Upload an unlocked copy.',
    'interactive_pdf':'Signed PDFs and interactive forms are not supported. Use an ordinary PDF.',
    'too_many_pages':'The selected PDFs exceed the 200-page limit.',
    'file_count':'Choose at least two files to merge, or exactly one for extraction or compression.',
    'page_selection':'Enter pages such as 1-3,5 or 3,1-2. Page numbers start at 1.',
    'page_out_of_range':'A selected page is outside the uploaded PDF. Check its page count.',
    'processing_limit':'The PDF could not finish within the processing limit. Try a smaller file.',
}


def file_fields(schema):
    return [name for name,field in schema.get('properties',{}).items() if field.get('format')==PDF_FORMAT]


def asset_ids(value):
    try:
        values=json.loads(value)
        if (not isinstance(values,list) or not 1<=len(values)<=8
                or any(not isinstance(item,str) or not ID.fullmatch(item) for item in values)
                or len(set(values))!=len(values)):
            raise ValueError()
        return values
    except (ValueError,TypeError):
        raise CapsuleError('Choose between 1 and 8 uploaded PDF files.','invalid_files') from None


def pdf_bytes(raw):
    return isinstance(raw,bytes) and len(raw)>=12 and raw.startswith(b'%PDF-') and b'%%EOF' in raw[-4096:]


class FileStore:
    def __init__(self,manager):
        self.manager=manager
        self.inbox=Path(manager.root)/'file-uploads'
        self.outbox=Path(manager.root)/'file-outbox'
        self.outputs=Path(manager.root)/'files'
        for path in (self.inbox,self.outbox,self.outputs):path.mkdir(mode=0o700,parents=True,exist_ok=True)
        with manager._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS file_uploads(
                    id TEXT PRIMARY KEY,share_id TEXT NOT NULL,name TEXT NOT NULL,
                    size INTEGER NOT NULL,expires_at REAL NOT NULL,job_id TEXT);
                CREATE TABLE IF NOT EXISTS file_outputs(
                    job_id TEXT PRIMARY KEY,size INTEGER NOT NULL);
            ''')

    def _path(self,directory,identifier):
        if not isinstance(identifier,str) or not ID.fullmatch(identifier):raise CapsuleError('File not found.','unavailable',404)
        path=directory/(identifier+'.pdf')
        if path.is_symlink() or path.resolve().parent!=directory.resolve():raise CapsuleError('File not found.','unavailable',404)
        return path

    @staticmethod
    def _storage(db):
        return db.execute('SELECT COALESCE(SUM(size),0) FROM file_uploads').fetchone()[0]+db.execute('SELECT COALESCE(SUM(size),0) FROM file_outputs').fetchone()[0]

    def upload(self,token,raw,name):
        share_id=self.manager._id(token)
        if len(raw)>MAX_FILE:raise CapsuleError('Each PDF must be at most 5 MB.','too_large',413)
        if not pdf_bytes(raw):raise CapsuleError('Choose a complete PDF file.','invalid_pdf')
        name=unicodedata.normalize('NFC',str(name)).replace('\\','/').rsplit('/',1)[-1]
        name=''.join(ch for ch in name if unicodedata.category(ch)[0]!='C')[:100].strip() or 'document.pdf'
        if not name.lower().endswith('.pdf'):name+='.pdf'
        identifier=secrets.token_hex(16)
        path=self._path(self.inbox,identifier)
        self.cleanup()
        with self.manager._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM shares WHERE id=?',(share_id,)).fetchone()
            if not row or self.manager._status(row)!='active':raise CapsuleError('This link has ended.','unavailable',410)
            if not file_fields(json.loads(row['schema_json'])):raise CapsuleError('This tool does not accept files.')
            count,total=db.execute('SELECT COUNT(*),COALESCE(SUM(size),0) FROM file_uploads WHERE share_id=?',(share_id,)).fetchone()
            if count>=8 or total+len(raw)>MAX_INPUT:raise CapsuleError('Use up to 8 PDFs and 20 MB in total. Remove a file before adding another.','file_quota',413)
            if self._storage(db)+len(raw)>MAX_STORAGE:raise CapsuleError('The file workspace is full. Try again later.','file_quota',503)
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            try:
                with os.fdopen(fd,'wb') as stream:stream.write(raw)
                expires=self.manager._clock()+UPLOAD_TTL
                db.execute('INSERT INTO file_uploads VALUES(?,?,?,?,?,NULL)',(identifier,share_id,name,len(raw),expires))
            except BaseException:
                path.unlink(missing_ok=True);raise
        return {'id':identifier,'name':name,'bytes':len(raw),'expires_at':expires}

    def remove(self,token,identifier):
        share_id=self.manager._id(token)
        path=self._path(self.inbox,identifier)
        with self.manager._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM file_uploads WHERE id=? AND share_id=?',(identifier,share_id)).fetchone()
            if not row:raise CapsuleError('File not found.','unavailable',404)
            if row['job_id']:raise CapsuleError('This file is already being processed.','busy',409)
            path.unlink(missing_ok=True)
            db.execute('DELETE FROM file_uploads WHERE id=?',(identifier,))

    def validate_inputs(self,db,row,args):
        identifiers=[]
        for name in file_fields(json.loads(row['schema_json'])):
            identifiers.extend(asset_ids(args.get(name)))
        if len(identifiers)>8 or len(set(identifiers))!=len(identifiers):raise CapsuleError('Choose between 1 and 8 uploaded PDF files.','invalid_files')
        for identifier in identifiers:
            upload=db.execute('SELECT * FROM file_uploads WHERE id=? AND share_id=?',(identifier,row['id'])).fetchone()
            if (not upload or upload['expires_at']<=self.manager._clock() or upload['job_id']
                    or not self._path(self.inbox,identifier).is_file()):
                raise CapsuleError('An uploaded file expired or is already in use. Select the files again.','invalid_files')
        return identifiers

    @staticmethod
    def lock_inputs(db,identifiers,job_id):
        db.executemany('UPDATE file_uploads SET job_id=? WHERE id=?',[(job_id,identifier) for identifier in identifiers])

    def finish_inputs(self,job_id):
        with self.manager._db() as db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute('SELECT id FROM file_uploads WHERE job_id=?',(job_id,)).fetchall():
                self._path(self.inbox,row['id']).unlink(missing_ok=True)
            db.execute('DELETE FROM file_uploads WHERE job_id=?',(job_id,))

    def save(self,job_id,response):
        value=unwrap_result(response)
        if (not isinstance(value,dict) or value.get('kind')!='file'
                or value.get('content_type')!='application/pdf'
                or not isinstance(value.get('filename'),str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\.pdf',value['filename'])
                or type(value.get('pages')) is not int or not 1<=value['pages']<=200
                or type(value.get('input_bytes')) is not int or not 0<value['input_bytes']<=MAX_INPUT):
            raise CapsuleError('The tool did not return a valid file receipt.')
        source=self._path(self.outbox,value.get('asset_id'))
        fd=os.open(source,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
        with os.fdopen(fd,'rb') as stream:raw=stream.read(MAX_OUTPUT+1)
        if len(raw)>MAX_OUTPUT or not pdf_bytes(raw):raise CapsuleError('The tool did not return a complete PDF file.')
        target=self._path(self.outputs,job_id)
        with self.manager._db() as db:
            db.execute('BEGIN IMMEDIATE')
            if self._storage(db)+len(raw)>MAX_STORAGE:raise CapsuleError('The file workspace is full. Try again later.','file_quota',503)
            fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            try:
                with os.fdopen(fd,'wb') as stream:stream.write(raw)
                db.execute('INSERT INTO file_outputs VALUES(?,?)',(job_id,len(raw)))
            except BaseException:
                target.unlink(missing_ok=True);raise
        source.unlink(missing_ok=True)
        with self.manager._db() as db:
            actual_input=db.execute('SELECT COALESCE(SUM(size),0) FROM file_uploads WHERE job_id=?',(job_id,)).fetchone()[0]
        return {'kind':'file','filename':value['filename'],'content_type':'application/pdf',
                'pages':value['pages'],'bytes':len(raw),'input_bytes':actual_input or value['input_bytes']}

    def path(self,job_id):return self._path(self.outputs,job_id)

    def purge(self,job_id):
        self.path(job_id).unlink(missing_ok=True)
        with self.manager._db() as db:db.execute('DELETE FROM file_outputs WHERE job_id=?',(job_id,))

    def cleanup(self):
        now=self.manager._clock()
        with self.manager._db() as db:
            db.execute('BEGIN IMMEDIATE')
            rows=db.execute('''SELECT u.id FROM file_uploads u JOIN shares s ON s.id=u.share_id
                LEFT JOIN image_jobs j ON j.id=u.job_id
                WHERE (u.job_id IS NULL AND u.expires_at<=?) OR
                (u.job_id IS NOT NULL AND j.state NOT IN ('queued','running')) OR
                (s.enabled=0 OR s.expires_at<=?) AND (u.job_id IS NULL OR j.state NOT IN ('queued','running'))''',(now,now)).fetchall()
            for row in rows:self._path(self.inbox,row['id']).unlink(missing_ok=True)
            db.executemany('DELETE FROM file_uploads WHERE id=?',[(row['id'],) for row in rows])
            known_inputs={row['id'] for row in db.execute('SELECT id FROM file_uploads')}
            known_outputs={row['job_id'] for row in db.execute('SELECT job_id FROM file_outputs')}
        # A process can stop between writing an artifact and saving its receipt.
        # Remove abandoned copies too, without touching registered live results.
        for directory,known in ((self.inbox,known_inputs),(self.outputs,known_outputs),(self.outbox,set())):
            for path in directory.glob('*.pdf'):
                if ID.fullmatch(path.stem) and path.stem not in known and not path.is_symlink():
                    try:
                        if path.stat().st_mtime+UPLOAD_TTL<=now:path.unlink(missing_ok=True)
                    except FileNotFoundError:pass
