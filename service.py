"""Profile service ownership: one lease, discoverable ports, safe attach/takeover."""
import json
import os
import threading
import time
from pathlib import Path
from urllib.request import Request, urlopen

try:
    from .image_provider import atomic_private, capture_profile_key_reader
except ImportError:
    from image_provider import atomic_private, capture_profile_key_reader


class ServiceLease:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.file = None

    def acquire(self):
        if self.file:
            return True
        handle = (self.root / "service.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"0"); handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close(); return False
        self.file = handle
        return True

    def release(self):
        if not self.file:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0); msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_UN)
        finally:
            self.file.close(); self.file = None


class Runtime:
    def __init__(self, root, dispatch, catalog, *, connection, port=9271, guest_port=9272, binary=None, generation_limit=30,
                 execution_limit=10000, connector=None, relay_url="", public_origin=""):
        self.root, self.dispatch = Path(root), dispatch
        try:
            from .catalog import reviewed_catalog
        except ImportError:
            from catalog import reviewed_catalog
        self.catalog = lambda: reviewed_catalog(catalog())
        self.connection, self.port, self.guest_port = connection, port, guest_port
        self.binary, self.generation_limit = binary, generation_limit
        self.execution_limit, self.connector = execution_limit, connector
        # Capture the owner profile before any monitor or HTTP worker can take over.
        self._secret_reader = capture_profile_key_reader()
        try:
            from .relay import normalise_base
        except ImportError:
            from relay import normalise_base
        self.relay_url = normalise_base(relay_url)
        self.public_origin = self._origin(public_origin)
        self.relay = None
        self.lease, self.stop_event, self._guard = ServiceLease(root), threading.Event(), threading.RLock()
        self.manager = self.jobs = self.public = None
        self.servers, self.threads = [], []
        self.info = {"service": "starting", "version": "0.5.2"}

    @staticmethod
    def _origin(value):
        """A creator-run fixed address (Tailscale Funnel, a Cloudflare named tunnel, ...)."""
        if not value:
            return ""
        from urllib.parse import urlsplit
        parts = urlsplit(str(value).strip())
        if parts.scheme != "https" or not parts.hostname or parts.path not in ("", "/") or parts.query or parts.fragment or parts.username:
            raise ValueError("public_origin must be an https origin such as https://my-pc.example.ts.net")
        return "https://" + parts.netloc

    def start(self):
        if self.lease.acquire():
            try:
                self._start_owned()
            except Exception:
                self.close(); raise
        else:
            self.info.update(service="attached")
            self._read_attached()
        monitor = threading.Thread(target=self._monitor, daemon=True, name="sharelet-service-lease")
        monitor.start()
        return self

    def _start_owned(self):
        try:
            from .sharelet import CapsuleManager
            from .jobs import ImageJobs
            from .media import MediaStore
            from .file_inputs import FileStore
            from .image_provider import ProviderSettings
            from .tunnel import PublicLink
            from .relay import RelayLink
            from .webserver import create_server
        except ImportError:
            from sharelet import CapsuleManager
            from jobs import ImageJobs
            from media import MediaStore
            from file_inputs import FileStore
            from image_provider import ProviderSettings
            from tunnel import PublicLink
            from relay import RelayLink
            from webserver import create_server
        self.manager = CapsuleManager(self.root, self.dispatch)
        provider = ProviderSettings(self.root, secret_reader=self._secret_reader)
        # A fixed address is run by the creator; otherwise open a quick tunnel on demand.
        self.public = None if self.public_origin else PublicLink(self.root, binary=self.binary)
        if self.public and self.relay_url:
            self.relay = RelayLink(self.root, self.relay_url, public_link=self.public)
        def require_tool(row):
            try:
                from .catalog import contract_digest
                from .sharelet import CapsuleError
            except ImportError:
                from catalog import contract_digest
                from sharelet import CapsuleError
            current = next((item for item in self.catalog() if item.get("supported")
                            and item["server"] == row["server"] and item["tool"] == row["tool"]
                            and (row["kind"] != "image" or item.get("style", "") == row["preset"])), None)
            if current is None:
                raise CapsuleError("This tool is unavailable or its server is no longer authorised. No run was charged.", "tool_unavailable", 503)
            if current.get('requires_side_effects_approval') and (not row['side_effects_approved'] or row['tool_review_hash'] != current['review_hash']):
                raise CapsuleError('This tool needs a fresh owner review before it can run. No run was charged.', 'tool_approval_required', 403)
            if row['tool_review_hash'] and row['tool_review_hash'] != current['review_hash']:
                raise CapsuleError('The reviewed tool changed. Ask the owner to create a new share. No run was charged.', 'tool_changed', 409)
            if contract_digest(current["schema"]) != contract_digest(json.loads(row["schema_json"])):
                raise CapsuleError("The tool's input rules changed. Ask the creator to review and create a new link. No run was charged.", "tool_changed", 409)
        self.jobs = ImageJobs(self.manager, MediaStore(self.root), require_ready=provider.require_ready,
                              require_tool=require_tool, limit=self.generation_limit, execution_limit=self.execution_limit,
                              files=FileStore(self.manager))
        self.jobs.recover_interrupted()
        self.jobs.cleanup_media()
        owner = create_server(self.manager, self.catalog, port=self.port, connection=self.connection,
                              jobs=self.jobs, public_link=self.public, provider=provider, auto_port=True, connector=self.connector,
                              relay=self.relay, public_origin=self.public_origin)
        self.servers.append(owner)
        guest = create_server(self.manager, lambda: [], port=self.guest_port, guest_only=True,
                              jobs=self.jobs, public_link=self.public, auto_port=True, relay=self.relay,
                              public_origin=self.public_origin)
        self.servers.append(guest)
        if self.public:
            self.public.guest_port = guest.server_port
            self._keep_public()
        if self.relay:
            self.relay.start()
        for server in self.servers:
            worker = threading.Thread(target=server.serve_forever, daemon=True, name="sharelet-http")
            worker.start(); self.threads.append(worker)
        self.info.update(service="listening", owner_url=f"http://127.0.0.1:{owner.server_port}/", owner_port=owner.server_port,
                         guest_port=guest.server_port, owner_entry_file=str(self.root / "owner-entry.txt"))
        atomic_private(self.root / "service.json", {"pid": os.getpid(), "owner_port": owner.server_port, "guest_port": guest.server_port, "version": "0.5.2"})
        owner_url = f"http://127.0.0.1:{owner.server_port}/#owner={self.manager.owner_token}"
        # This file intentionally contains the owner credential, not agent tool output.
        fd = os.open(self.root / "owner-entry.txt", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(owner_url)

    def _read_attached(self):
        try:
            metadata = json.loads((self.root / "service.json").read_text())
            token = (self.root / "owner-v2.key").read_text()
            port = metadata.get("owner_port")
            if type(port) is not int or not 1024 <= port <= 65535:
                return
            try:
                from .owner_client import verify_local_service
            except ImportError:
                from owner_client import verify_local_service
            verify_local_service(f'http://127.0.0.1:{port}', self.root.resolve(), token)
            with urlopen(Request(f"http://127.0.0.1:{port}/api/owner/ping", headers={"Authorization": "Bearer " + token}), timeout=2) as response:
                info = json.load(response)
            if info.get("ok") and info.get("profile_id") == str(self.root.resolve()):
                self.info.update(service="attached", owner_url=f"http://127.0.0.1:{port}/", owner_port=port,
                                 guest_port=metadata.get("guest_port"), owner_entry_file=str(self.root / "owner-entry.txt"))
        except Exception:
            pass

    def _monitor(self):
        while not self.stop_event.wait(1):
            with self._guard:
                if self.stop_event.is_set():
                    return
                if not self.lease.file:
                    if self.lease.acquire():
                        try:
                            self._start_owned()
                        except Exception:
                            self.info.update(service="unavailable")
                            self.close()
                            return
                    else:
                        self._read_attached()
                elif self.jobs:
                    try:
                        self.jobs.cleanup_media()
                    except OSError:
                        pass
                    self._ticks = getattr(self, "_ticks", 0) + 1
                    if self._ticks % 5 == 0:
                        self._keep_public()

    def _keep_public(self):
        """Keep the guest address open exactly while a public link can still be used."""
        if not self.public or not self.manager:
            return
        try:
            needed = self.manager.has_active_public()
            if needed and not self.public.wanted:
                self.public.start()
            elif not needed and self.public.wanted:
                self.public.stop()
        except Exception:
            pass

    def close(self):
        self.stop_event.set()
        with self._guard:
            if self.relay:
                self.relay.stop(); self.relay = None
            if self.public:
                self.public.stop()
            if self.jobs:
                self.jobs.close()
            for server in self.servers:
                if self.threads:
                    server.shutdown()
                server.server_close()
            for worker in self.threads:
                worker.join(timeout=3)
            self.servers.clear(); self.threads.clear()
            self.lease.release()
