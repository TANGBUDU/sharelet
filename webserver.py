"""Owner workspace, capability-scoped guest API, image delivery and public control."""
from __future__ import annotations

from collections import defaultdict, deque
import errno
import hmac
import json
import os
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs, unquote

try:
    from .sharelet import CapsuleError, MAX_REQ, _same_json, local_identity_proof
    from .distribution import build_plugin_archive
    from .sharing import ShareWorkflow
    from .catalog import reviewed_catalog
except ImportError:
    from sharelet import CapsuleError, MAX_REQ, _same_json, local_identity_proof
    from distribution import build_plugin_archive
    from sharing import ShareWorkflow
    from catalog import reviewed_catalog

WEB = Path(__file__).parent / "web"
ASSETS = {"/assets/"+name:(name,kind) for name,kind in [
    ("app.js","text/javascript; charset=utf-8"),("i18n.js","text/javascript; charset=utf-8"),
    ("forms.js","text/javascript; charset=utf-8"),("owner.js","text/javascript; charset=utf-8"),
    ("guest.js","text/javascript; charset=utf-8"),("canvas-demo.js","text/javascript; charset=utf-8"),
    ("files.js","text/javascript; charset=utf-8"),
    ("weather.js","text/javascript; charset=utf-8"),("weather.css","text/css; charset=utf-8"),
    ("style.css","text/css; charset=utf-8"),("files.css","text/css; charset=utf-8"),("icon.svg","image/svg+xml")]}
TOKEN = r"([a-f0-9]{24}\.[A-Za-z0-9_-]{43})"
JOB = r"([a-f0-9]{32})"
JSON_BODY_SECONDS = 10
JSON_SLOTS = 8
JSON_PER_LINK = 2
UPLOAD_BODY_SECONDS = 90
UPLOAD_SLOTS = 4
UPLOAD_PER_LINK = 2
UPLOAD_PER_LINK_MINUTE = 30
UPLOAD_GLOBAL_MINUTE = 240


class CapabilityLimiter:
    """Per verified link, plus an explicit service ceiling; never proxy client IP."""
    def __init__(self, per_link=10, global_limit=120, clock=time.monotonic):
        self.per_link, self.global_limit, self.clock = per_link, global_limit, clock
        self._links, self._all, self._lock = defaultdict(deque), deque(), threading.Lock()

    def admit(self, share_id):
        now = self.clock()
        with self._lock:
            while self._all and self._all[0] <= now-60:
                self._all.popleft()
            for key in list(self._links):
                hits = self._links[key]
                while hits and hits[0] <= now-60:
                    hits.popleft()
                if not hits:
                    del self._links[key]
            hits = self._links[share_id]
            if len(hits) >= self.per_link or len(self._all) >= self.global_limit:
                return False
            hits.append(now); self._all.append(now)
            return True


class ExclusiveHTTPServer(ThreadingHTTPServer):
    # POSIX reuse permits clean restarts, not a second live listener. Windows
    # requires the exclusive option below instead of SO_REUSEADDR.
    allow_reuse_address = os.name != "nt"
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, *args, **kwargs):
        self._connections = threading.BoundedSemaphore(64)
        super().__init__(*args, **kwargs)

    def handle_error(self, request, address):
        # Clients closing or resetting a connection is routine, not a server fault.
        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        super().handle_error(request, address)

    def process_request(self, request, address):
        if not self._connections.acquire(blocking=False):
            try:
                request.settimeout(.2)
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self._connections.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self._connections.release()

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def create_server(manager, catalog, *, port=0, guest_only=False, connection=None,
                  public_origin="", allowed_hosts=None, jobs=None, public_link=None,
                  provider=None, auto_port=False, limiter=None, connector=None, relay=None):
    fixed_origin = public_origin.rstrip("/")
    limiter = limiter or CapabilityLimiter()
    # Uploads and removals get their own allowance so file churn cannot spend run allowance.
    upload_limiter = CapabilityLimiter(per_link=UPLOAD_PER_LINK_MINUTE, global_limit=UPLOAD_GLOBAL_MINUTE)
    allowed = {"localhost","127.0.0.1","::1"} | set(allowed_hosts or [])
    # Processing slots are taken only after a request body has fully arrived.
    slots = threading.BoundedSemaphore(8)
    json_slots = threading.BoundedSemaphore(JSON_SLOTS)
    json_active, json_guard = {}, threading.Lock()
    # Large PDF bodies are read while holding a separate, smaller pool, capped per link.
    upload_slots = threading.BoundedSemaphore(UPLOAD_SLOTS)
    upload_active, upload_guard = defaultdict(int), threading.Lock()
    catalog_reader = catalog
    catalog = lambda: reviewed_catalog(catalog_reader())
    workflow = ShareWorkflow(manager, catalog, provider)

    def origin():
        if public_link:
            state = public_link.status()
            return state["origin"] if state["state"] == "live" else ""
        return fixed_origin

    def share_url(token, public_enabled, host):
        """Relay links stay the same across reconnects; tunnel links change with the origin."""
        if public_enabled and relay and relay.ready:
            return relay.link(token)
        base = origin() if public_enabled else ""
        return (base or "http://"+host)+"/s/"+token

    def public_ready():
        return bool((relay and relay.ready) or origin())

    def want_public():
        # Creating a public link starts (or keeps) the guest address without a manual step.
        if public_link and not public_link.wanted:
            try:
                public_link.start()
            except Exception:
                pass

    class Handler(BaseHTTPRequestHandler):
        server_version = "Sharelet/0.5.2"

        def setup(self):
            super().setup(); self.connection.settimeout(20)

        def log_message(self, *args):
            pass

        def _reply(self, status, payload, kind="application/json; charset=utf-8", *, filename=None):
            body = payload if isinstance(payload, bytes) else json.dumps(payload,ensure_ascii=False,allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type",kind); self.send_header("Content-Length",str(len(body)))
            self.send_header("Cache-Control","no-store"); self.send_header("Referrer-Policy","no-referrer")
            self.send_header("X-Content-Type-Options","nosniff"); self.send_header("X-Frame-Options","SAMEORIGIN")
            self.send_header("Content-Security-Policy","default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; frame-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'self'; object-src 'none'")
            if filename:
                self.send_header("Content-Disposition",'attachment; filename="'+filename+'"')
            try:
                self.end_headers()
                self.wfile.write(body)
            except ConnectionError:
                # The client went away (including Windows' ConnectionAbortedError).
                self.close_connection = True

        def _host_ok(self):
            try:
                host = urlsplit("http://"+self.headers.get("Host","")).hostname
                extra = public_link.origin if public_link else fixed_origin
                return host in allowed or bool(extra and host==urlsplit(extra).hostname)
            except ValueError:
                return False

        def _owner(self):
            value=self.headers.get("Authorization","")
            return not guest_only and hmac.compare_digest(value.encode(),("Bearer "+manager.owner_token).encode())

        def _require_agent(self):
            value = self.headers.get('Authorization', '')
            if guest_only or not hmac.compare_digest(value.encode(), ('Bearer '+manager.agent_token).encode()):
                raise CapsuleError('This controller credential has no access here.', 'unauthorized', 401)

        def _shares(self, visible=None):
            shares = [row for row in manager.list_shares() if visible is None or row['id'] in visible]
            for share in shares:
                token = share.pop('token')
                public = share.get('public_enabled', True)
                share.update(url=share_url(token, public, self.headers['Host']), local_url='/s/'+token,
                             public=public and public_ready(), online=public and bool(origin()))
            return shares

        def _origin_ok(self):
            value=self.headers.get("Origin")
            if not value:return True
            try:
                parsed=urlsplit(value)
                return parsed.scheme in ("http","https") and parsed.netloc==self.headers.get("Host")
            except ValueError:return False

        def _read_body(self, length, seconds):
            """Read exactly length bytes within one overall deadline, not per received chunk."""
            deadline, chunks, received = time.monotonic()+seconds, [], 0
            try:
                while received < length:
                    remaining = deadline-time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    self.connection.settimeout(remaining)
                    chunk = self.rfile.read1(min(65536, length-received))
                    if not chunk:
                        raise CapsuleError("The request was incomplete.")
                    chunks.append(chunk); received += len(chunk)
            except (TimeoutError, socket.timeout):
                self.close_connection = True
                raise CapsuleError("The request took too long to arrive. Please try again.", "request_timeout", 408) from None
            finally:
                try:
                    self.connection.settimeout(20)
                except OSError:
                    pass
            return b"".join(chunks)

        def _claim_json(self, share_id):
            with json_guard:
                if share_id is not None and json_active.get(share_id, 0) >= JSON_PER_LINK:
                    raise CapsuleError('This link is already sending requests. Wait for them to finish.', 'busy', 429)
                if not json_slots.acquire(blocking=False):
                    raise CapsuleError('The service is busy. Please try again shortly.', 'busy', 503)
                if share_id is not None:
                    json_active[share_id] = json_active.get(share_id, 0) + 1

        def _release_json(self, share_id):
            with json_guard:
                if share_id is not None:
                    json_active[share_id] -= 1
                    if not json_active[share_id]:
                        del json_active[share_id]
                json_slots.release()

        def _json(self, share_id=None):
            if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length",[]))!=1:
                raise CapsuleError("Invalid request framing.")
            if self.headers.get("Content-Type","").split(";")[0].strip().lower()!="application/json":
                raise CapsuleError("Use a JSON request.","invalid_request",415)
            try:
                length=int(self.headers.get("Content-Length","0"))
                if not 1<=length<=MAX_REQ:raise CapsuleError("The submitted content is too large.","too_large",413)
                self._claim_json(share_id)
                try:
                    raw=self._read_body(length,JSON_BODY_SECONDS)
                finally:
                    self._release_json(share_id)
                def pairs(items):
                    data={}
                    for key,value in items:
                        if key in data:raise ValueError("duplicate key")
                        data[key]=value
                    return data
                value=json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError("non-finite")))
            except (ValueError,UnicodeError,RecursionError):
                raise CapsuleError("Invalid JSON.") from None
            if not isinstance(value,dict):raise CapsuleError("Expected a form object.")
            return value

        def _require_owner(self):
            if not self._owner():raise CapsuleError("Open this page using your private creator entry.","unauthorized",401)

        def _job(self, token, job_id):
            if not jobs:raise CapsuleError("Image generation is unavailable.","unavailable",503)
            result=jobs.get(token,job_id)
            if result["state"]=="succeeded" and result.get("kind")=="image":
                result["media_url"]="/api/s/"+token+"/jobs/"+job_id+"/media"
            elif result["state"]=="succeeded":
                result["result_url"]="/api/s/"+token+"/jobs/"+job_id+"/result"
                if isinstance(result.get('result'),dict) and result['result'].get('kind')=='file' and jobs.files:
                    result['file_url']='/api/s/'+token+'/jobs/'+job_id+'/file'
            return result

        def _files(self):
            if not jobs or not jobs.files:raise CapsuleError('File uploads are unavailable.','unavailable',503)
            return jobs.files

        def _upload_length(self):
            """Validate upload framing from headers alone, before any slot or allowance is used."""
            try:
                from .file_inputs import MAX_FILE
            except ImportError:
                from file_inputs import MAX_FILE
            if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length',[]))!=1:
                raise CapsuleError('Invalid request framing.')
            if self.headers.get('Content-Type','').split(';')[0].strip().lower()!='application/pdf':
                raise CapsuleError('Choose a PDF file.','invalid_pdf',415)
            try:length=int(self.headers.get('Content-Length','0'))
            except ValueError:raise CapsuleError('Invalid request framing.') from None
            if not 1<=length<=MAX_FILE:raise CapsuleError('Each PDF must be at most 5 MB.','too_large',413)
            return length

        def _claim_upload(self, share_id):
            with upload_guard:
                if upload_active[share_id] >= UPLOAD_PER_LINK:
                    raise CapsuleError('This link is already uploading files. Wait for them to finish.','busy',429)
                if not upload_slots.acquire(blocking=False):
                    raise CapsuleError('The service is busy. Please try again shortly.','busy',503)
                upload_active[share_id] += 1

        def _release_upload(self, share_id):
            with upload_guard:
                upload_active[share_id] -= 1
                if upload_active[share_id] <= 0:
                    del upload_active[share_id]
                upload_slots.release()

        def _share(self, token):
            info=manager.public_info(token)
            if guest_only and not info.get("public_enabled",True):
                raise CapsuleError("This page does not exist.","unavailable",404)
            return info

        def do_GET(self):
            try:
                if not self._host_ok():raise CapsuleError("Unrecognised host.","invalid_host",421)
                parsed=urlsplit(self.path); path=parsed.path
                scoped=re.match(r"/(?:api/)?s/"+TOKEN+r"(?:/|$)",path)
                if scoped:self._share(scoped[1])
                if path=="/healthz":
                    self._reply(200,{"ok":True,"version":"0.5.2","guest_only":guest_only,
                                     **({"relay_host":relay.host_id} if relay else {})});return
                if path=='/api/local/identify':
                    if guest_only:raise CapsuleError('This endpoint is private.', 'unauthorized', 401)
                    query=parse_qs(parsed.query)
                    if set(query)!={'challenge','role'} or any(len(value)!=1 for value in query.values()):
                        raise CapsuleError('Invalid identity challenge.')
                    challenge,role=query['challenge'][0],query['role'][0]
                    if not re.fullmatch(r'[a-f0-9]{48}',challenge) or role not in ('owner','agent'):
                        raise CapsuleError('Invalid identity challenge.')
                    token=manager.owner_token if role=='owner' else manager.agent_token
                    profile=str(manager.root.resolve())
                    self._reply(200,{'ok':True,'profile_id':profile,'proof':local_identity_proof(token,profile,challenge,role,self.server.server_port)});return
                if path in ASSETS:
                    file,kind=ASSETS[path];self._reply(200,(WEB/file).read_bytes(),kind);return
                if path=="/" and not guest_only:
                    self._reply(200,(WEB/"index.html").read_bytes(),"text/html; charset=utf-8");return
                match=re.fullmatch(r"/s/"+TOKEN,path)
                if match:
                    manager.public_info(match[1]);self._reply(200,(WEB/"guest.html").read_bytes(),"text/html; charset=utf-8");return
                if path=="/api/owner/ping":
                    self._require_owner();self._reply(200,{"ok":True,"profile_id":str(manager.root.resolve())});return
                if path=='/api/agent/ping':
                    self._require_agent();self._reply(200,{'ok':True,'profile_id':str(manager.root.resolve())});return
                if path=='/api/agent/state':
                    self._require_agent()
                    self._reply(200,{'ok':True,'tools':catalog(),'drafts':workflow.list_drafts(),
                        'shares':self._shares(workflow.agent_share_ids()),'connection':connection,
                        'connections':connector.connection_status() if connector else None,
                        'image_provider_configured':bool(provider and provider.public()['configured']),
                        'budget':jobs.budget() if jobs else None,'usage':jobs.usage() if jobs else None});return
                if path=="/api/owner/state":
                    self._require_owner()
                    base=origin() or "http://"+self.headers["Host"]
                    shares=self._shares()
                    self._reply(200,{"ok":True,"shares":shares,"tools":catalog(),"activity":manager.activity(),
                                     "drafts":workflow.list_drafts(pending=True),
                                     "connection":connection or {"mode":"local","label":"Local preview"},
                                     "public_origin":origin(),"public_link":public_link.status() if public_link else {"state":"live" if fixed_origin else "offline","origin":fixed_origin},
                                     "provider":provider.public() if provider else None,"budget":jobs.budget() if jobs else None,
                                     "usage":jobs.usage() if jobs else None,
                                     "connections":connector.connection_status() if connector else None,
                                     "relay":relay.status() if relay else None,"stable_links":bool((relay and relay.ready) or fixed_origin),"version":"0.5.2"});return
                match=re.fullmatch(r"/api/s/"+TOKEN+r"/jobs/"+JOB+r"/media",path)
                if match:
                    job=self._job(match[1],match[2])
                    if job["state"]!="succeeded":raise CapsuleError("The image is not ready.","not_ready",409)
                    image=jobs.media.path(match[2])
                    if not image.is_file():raise CapsuleError("This image is no longer available.","unavailable",404)
                    self._reply(200,image.read_bytes(),"image/png",filename="sharelet-image.png" if "download" in parse_qs(parsed.query) else None);return
                match=re.fullmatch(r'/api/s/'+TOKEN+r'/jobs/'+JOB+r'/file',path)
                if match:
                    job=self._job(match[1],match[2])
                    if job['state']!='succeeded' or not job.get('file_url'):raise CapsuleError('The file is not ready.','not_ready',409)
                    output=self._files().path(match[2])
                    if not output.is_file():raise CapsuleError('This file is no longer available.','unavailable',404)
                    self._reply(200,output.read_bytes(),'application/pdf',filename=job['result']['filename']);return
                match=re.fullmatch(r"/api/s/"+TOKEN+r"/jobs/"+JOB+r"/result",path)
                if match:
                    job=self._job(match[1],match[2])
                    if job["state"]!="succeeded" or job.get("kind")!="tool":raise CapsuleError("The result is not ready.","not_ready",409)
                    self._reply(200,job.get("result"),filename="sharelet-result.json" if "download" in parse_qs(parsed.query) else None);return
                match=re.fullmatch(r"/api/s/"+TOKEN+r"/jobs/"+JOB,path)
                if match:
                    self._reply(200,{"ok":True,"job":self._job(match[1],match[2]),"share":manager.public_info(match[1])});return
                match=re.fullmatch(r"/api/s/"+TOKEN+r"/requests/([A-Za-z0-9_-]{20,80})",path)
                if match:
                    if not jobs:raise CapsuleError("Image generation is unavailable.","unavailable",503)
                    job=jobs.by_request(match[1],match[2])
                    self._reply(200,{"ok":True,"job":self._job(match[1],job["id"]),"share":manager.public_info(match[1])});return
                if path=="/download/sharelet.zip":
                    self._reply(200,build_plugin_archive(),"application/zip",filename="sharelet-plugin.zip");return
                match=re.fullmatch(r"/api/s/"+TOKEN,path)
                if match:
                    self._reply(200,{"ok":True,"share":manager.public_info(match[1])});return
                raise CapsuleError("This page does not exist.","unavailable",404)
            except CapsuleError as error:
                self._reply(error.status,{"ok":False,"error":str(error),"code":error.code})
            except Exception:
                self._reply(503,{"ok":False,"error":"The creator's service is temporarily unavailable."})

        def do_POST(self):
            acquired=False
            try:
                if not self._host_ok() or not self._origin_ok():raise CapsuleError("Invalid request origin.","invalid_origin",403)
                path=urlsplit(self.path).path
                upload=re.fullmatch(r'/api/s/'+TOKEN+r'/uploads(?:/([a-f0-9]{32})/remove)?',path)
                if upload:
                    info=self._share(upload[1])
                    if info['status']!='active':raise CapsuleError('This link has ended.','unavailable',410)
                    files=self._files()
                    upload_share=manager._id(upload[1])
                    if upload[2]:
                        body=self._json(upload_share)
                        if body:raise CapsuleError('Unexpected removal settings.')
                        if not upload_limiter.admit(upload_share):
                            raise CapsuleError('This link is changing files too quickly. Please wait a moment.','rate_limited',429)
                        acquired=slots.acquire(blocking=False)
                        if not acquired:raise CapsuleError('The service is busy. Please try again shortly.','busy',503)
                        files.remove(upload[1],upload[2]);self._reply(200,{'ok':True});return
                    length=self._upload_length()
                    if not upload_limiter.admit(upload_share):
                        raise CapsuleError('This link is uploading files too quickly. Please wait a moment.','rate_limited',429)
                    self._claim_upload(upload_share)
                    try:
                        raw=self._read_body(length,UPLOAD_BODY_SECONDS)
                    finally:
                        self._release_upload(upload_share)
                    acquired=slots.acquire(blocking=False)
                    if not acquired:raise CapsuleError('The service is busy. Please try again shortly.','busy',503)
                    result=files.upload(upload[1],raw,unquote(self.headers.get('X-Sharelet-Filename','document.pdf')))
                    self._reply(201,{'ok':True,'upload':result});return
                if path.startswith('/api/agent/'):
                    self._require_agent();body=self._json()
                    if path=='/api/agent/drafts':
                        self._reply(202,{'ok':True,'draft':workflow.propose(body),
                                        'message':'Pending owner approval in the private Sharelet workspace. No link or public address was opened.'});return
                    match=re.fullmatch(r'/api/agent/shares/([a-f0-9]{24})/revoke',path)
                    if match and not body and match[1] in workflow.agent_share_ids():
                        manager.revoke(match[1]);self._reply(200,{'ok':True});return
                    raise CapsuleError('This controller cannot approve, publish or change owner settings.', 'forbidden', 403)
                if path.startswith("/api/owner/"):
                    self._require_owner();body=self._json()
                    if path=="/api/owner/connections/inspect":
                        if not connector or not hasattr(connector,"inspect_config"):raise CapsuleError("Manage connections in your Hermes profile.")
                        self._reply(200,{"ok":True,"connections":connector.inspect_config(body)});return
                    if path=="/api/owner/connections":
                        if not connector or not hasattr(connector,"save_connections"):raise CapsuleError("Manage connections in your Hermes profile.")
                        self._reply(200,{"ok":True,"connections":connector.save_connections(body)});return
                    if path=="/api/owner/tools/refresh":
                        if connector and hasattr(connector,"refresh"):connector.refresh()
                        self._reply(200,{"ok":True,"tools":catalog()});return
                    if path=="/api/owner/provider":
                        if not provider:raise CapsuleError("Provider setup is unavailable.")
                        self._reply(200,{"ok":True,"provider":provider.save(body)});return
                    if path=='/api/owner/limits':
                        if not jobs:raise CapsuleError('Run allowances are unavailable.')
                        self._reply(200,{'ok':True,**jobs.set_limits(body)});return
                    if path in ("/api/owner/public/start","/api/owner/public/stop"):
                        if not public_link:raise CapsuleError("The public connector is unavailable.")
                        status=public_link.start() if path.endswith("/start") else public_link.stop()
                        self._reply(200,{"ok":True,"public_link":status});return
                    if path=="/api/owner/shares":
                        if isinstance(body,dict) and body.get('public') is True and not public_ready():
                            want_public()  # open the address now; the creator page waits for it
                        token=workflow.create_owned(body,public_ready())
                        public=manager.public_info(token)['public_enabled']
                        if public:want_public()
                        self._reply(201,{"ok":True,"id":token.split(".")[0],"url":share_url(token,public,self.headers['Host'])});return
                    match=re.fullmatch(r'/api/owner/drafts/([a-f0-9]{24})/(approve|reject)',path)
                    if match:
                        if match[2]=='reject':
                            if body:raise CapsuleError('Unexpected rejection settings.')
                            workflow.reject(match[1]);self._reply(200,{'ok':True});return
                        token=workflow.approve(match[1],body,public_ready())
                        public=manager.public_info(token)['public_enabled']
                        if public:want_public()
                        self._reply(200,{'ok':True,'id':token.split('.')[0],'url':share_url(token,public,self.headers['Host'])});return
                    match=re.fullmatch(r"/api/owner/shares/([a-f0-9]{24})/revoke",path)
                    if match:
                        manager.revoke(match[1]);self._reply(200,{"ok":True});return
                    raise CapsuleError("This endpoint does not exist.","unavailable",404)
                match=re.fullmatch(r"/api/s/"+TOKEN+r"(/jobs)?",path)
                if not match:raise CapsuleError("This endpoint does not exist.","unavailable",404)
                share_id=manager._id(match[1])
                info=self._share(match[1])
                if info.get("kind")=="image" and not match[2]:
                    raise CapsuleError("Image recipes require an idempotent image job.","image_job_required",400)
                if jobs and not match[2]:
                    raise CapsuleError("Use an idempotent tool job for this link.","tool_job_required",400)
                body=self._json(share_id)
                acquired=slots.acquire(blocking=False)
                if not acquired:raise CapsuleError("The studio is busy. Please try again shortly.","busy",503)
                if match[2]:
                    if not jobs:raise CapsuleError("Image generation is unavailable.","unavailable",503)
                    if set(body)!={"input","request_id"}:raise CapsuleError("Invalid image request fields.")
                    job=jobs.submit(match[1],body["input"],body["request_id"],admit=limiter.admit)
                    self._reply(202,{"ok":True,"job":self._job(match[1],job["id"]),"share":manager.public_info(match[1])});return
                result=manager.invoke(match[1],body,admit=limiter.admit)
                self._reply(200,{"ok":True,"result":result,"share":manager.public_info(match[1])})
            except CapsuleError as error:
                self._reply(error.status,{"ok":False,"error":str(error),"code":error.code})
            except Exception:
                self._reply(502,{"ok":False,"error":"The request could not be completed."})
            finally:
                if acquired:slots.release()

    try:
        return ExclusiveHTTPServer(("127.0.0.1",port),Handler)
    except OSError as error:
        if not auto_port or not port or (error.errno not in (errno.EADDRINUSE,errno.EACCES) and getattr(error,"winerror",None) not in (10048,10013)):
            raise
        return ExclusiveHTTPServer(("127.0.0.1",0),Handler)
