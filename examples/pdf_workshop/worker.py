"""Bounded subprocess for PDF parsing; accepts only opaque local upload IDs."""
import io
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sys

MEMORY_LIMIT=768*1024*1024
CPU_SECONDS=25


def limit_windows_process():
    """Put this worker in its own Job Object with the same memory and CPU caps as POSIX."""
    import ctypes
    from ctypes import wintypes
    class Basic(ctypes.Structure):
        _fields_=[('PerProcessUserTimeLimit',ctypes.c_int64),('PerJobUserTimeLimit',ctypes.c_int64),
                  ('LimitFlags',wintypes.DWORD),('MinimumWorkingSetSize',ctypes.c_size_t),
                  ('MaximumWorkingSetSize',ctypes.c_size_t),('ActiveProcessLimit',wintypes.DWORD),
                  ('Affinity',ctypes.c_size_t),('PriorityClass',wintypes.DWORD),('SchedulingClass',wintypes.DWORD)]
    class Counters(ctypes.Structure):
        _fields_=[(name,ctypes.c_uint64) for name in ('ReadOperationCount','WriteOperationCount','OtherOperationCount',
                                                       'ReadTransferCount','WriteTransferCount','OtherTransferCount')]
    class Extended(ctypes.Structure):
        _fields_=[('BasicLimitInformation',Basic),('IoInfo',Counters),('ProcessMemoryLimit',ctypes.c_size_t),
                  ('JobMemoryLimit',ctypes.c_size_t),('PeakProcessMemoryUsed',ctypes.c_size_t),('PeakJobMemoryUsed',ctypes.c_size_t)]
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateJobObjectW.restype=wintypes.HANDLE
    kernel.CreateJobObjectW.argtypes=[ctypes.c_void_p,wintypes.LPCWSTR]
    kernel.SetInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes=[wintypes.HANDLE,wintypes.HANDLE]
    kernel.GetCurrentProcess.restype=wintypes.HANDLE
    job=kernel.CreateJobObjectW(None,None)
    if not job:raise OSError(ctypes.get_last_error(),'CreateJobObject failed')
    info=Extended()
    # PROCESS_TIME | PROCESS_MEMORY | KILL_ON_JOB_CLOSE
    info.BasicLimitInformation.LimitFlags=0x2|0x100|0x2000
    info.BasicLimitInformation.PerProcessUserTimeLimit=CPU_SECONDS*10_000_000
    info.ProcessMemoryLimit=MEMORY_LIMIT
    if not kernel.SetInformationJobObject(job,9,ctypes.byref(info),ctypes.sizeof(info)):
        raise OSError(ctypes.get_last_error(),'SetInformationJobObject failed')
    if not kernel.AssignProcessToJobObject(job,kernel.GetCurrentProcess()):
        raise OSError(ctypes.get_last_error(),'AssignProcessToJobObject failed')
    return job  # Keep the handle open for the life of the worker.


if os.name=='posix':
    import resource
    resource.setrlimit(resource.RLIMIT_AS,(MEMORY_LIMIT,MEMORY_LIMIT))
    resource.setrlimit(resource.RLIMIT_CPU,(CPU_SECONDS,CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_FSIZE,(25*1024*1024,25*1024*1024))
elif os.name=='nt':
    try:
        _JOB=limit_windows_process()
    except OSError:
        # Fail closed: never parse untrusted PDFs without a memory cap.
        print(json.dumps({'kind':'file_error','code':'processing_limit'}));sys.exit(0)

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from file_inputs import asset_ids,MAX_FILE,MAX_INPUT,pdf_bytes
from pypdf import PdfReader,PdfWriter

logging.getLogger('pypdf').setLevel(logging.CRITICAL)


class InvalidPDF(Exception):pass


def selected_pages(value,count):
    if not isinstance(value,str) or not 1<=len(value)<=200:raise InvalidPDF('page_selection')
    result=[]
    for piece in value.split(','):
        match=re.fullmatch(r'\s*([1-9][0-9]{0,2})(?:\s*-\s*([1-9][0-9]{0,2}))?\s*',piece)
        if not match:raise InvalidPDF('page_selection')
        first,last=int(match[1]),int(match[2] or match[1])
        if max(first,last)>count:raise InvalidPDF('page_out_of_range')
        result.extend(range(first-1,last if last>=first else last-2,1 if last>=first else -1))
        if len(result)>200:raise InvalidPDF('too_many_pages')
    return result


def process(spec):
    identifiers=asset_ids(spec.get('files'))
    action=spec.get('action')
    if action not in ('Merge PDFs','Extract pages','Lossless compression'):raise InvalidPDF('invalid_pdf')
    if (action=='Merge PDFs' and len(identifiers)<2) or (action!='Merge PDFs' and len(identifiers)!=1):raise InvalidPDF('file_count')
    root=Path(os.environ.get('SHARELET_FILE_WORKSPACE',''))
    if not root.is_absolute() or not root.is_dir():raise InvalidPDF('invalid_pdf')
    inbox=root/'file-uploads';outbox=root/'file-outbox'
    inputs=[];total=0;page_count=0
    for identifier in identifiers:
        path=inbox/(identifier+'.pdf')
        if path.is_symlink() or path.resolve().parent!=inbox.resolve():raise InvalidPDF('invalid_pdf')
        fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
        with os.fdopen(fd,'rb') as stream:raw=stream.read(MAX_FILE+1)
        total+=len(raw)
        if len(raw)>MAX_FILE or total>MAX_INPUT or not pdf_bytes(raw):raise InvalidPDF('invalid_pdf')
        reader=PdfReader(io.BytesIO(raw),strict=True)
        if reader.is_encrypted:raise InvalidPDF('encrypted_pdf')
        if reader.trailer['/Root'].get('/AcroForm'):raise InvalidPDF('interactive_pdf')
        count=len(reader.pages)
        if count==0:raise InvalidPDF('invalid_pdf')
        page_count+=count
        if page_count>200:raise InvalidPDF('too_many_pages')
        for page in reader.pages:
            if any(annotation.get_object().get('/Subtype')=='/Widget' for annotation in page.get('/Annots',[])):
                raise InvalidPDF('interactive_pdf')
        inputs.append(reader)
    writer=PdfWriter()
    if action=='Extract pages':
        for index in selected_pages(spec.get('pages'),len(inputs[0].pages)):writer.add_page(inputs[0].pages[index])
    else:
        for reader in inputs:
            for page in reader.pages:writer.add_page(page)
    if action=='Lossless compression':
        for page in writer.pages:page.compress_content_streams(level=9)
        writer.compress_identical_objects(remove_duplicates=True,remove_unreferenced=True)
    count=len(writer.pages)
    outbox.mkdir(mode=0o700,exist_ok=True)
    identifier=secrets.token_hex(16)
    target=outbox/(identifier+'.pdf')
    try:
        fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as stream:writer.write(stream)
        # Reopen the actual artifact before returning a receipt.
        if len(PdfReader(target,strict=True).pages)!=count:raise InvalidPDF('invalid_pdf')
        return {'kind':'file','asset_id':identifier,'content_type':'application/pdf','pages':count,
                'input_bytes':total,'filename':{'Merge PDFs':'merged.pdf','Extract pages':'selected-pages.pdf',
                    'Lossless compression':'compressed.pdf'}[action]}
    except BaseException:
        target.unlink(missing_ok=True);raise


if __name__=='__main__':
    try:
        raw=sys.stdin.buffer.read(4097)
        if len(raw)>4096:raise InvalidPDF('invalid_pdf')
        value=process(json.loads(raw))
    except InvalidPDF as error:value={'kind':'file_error','code':str(error)}
    except MemoryError:value={'kind':'file_error','code':'processing_limit'}
    except Exception:value={'kind':'file_error','code':'invalid_pdf'}
    print(json.dumps(value))
