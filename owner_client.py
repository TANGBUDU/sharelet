"""Local owner client used by MCP integrations. Never exports the creator credential."""
from pathlib import Path
import json
import os
import subprocess
import sys
import time
import secrets
import hmac
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

try:
    from .service import ServiceLease
    from .sharelet import CapsuleError, local_identity_proof
except ImportError:
    from service import ServiceLease
    from sharelet import CapsuleError, local_identity_proof


def verify_local_service(base, root, key, role='owner'):
    challenge = secrets.token_hex(24)
    # Prove possession of the workspace credential before sending it to a port
    # from stale metadata. An unrelated local listener receives no credential.
    with urlopen(base+'/api/local/identify?role='+role+'&challenge='+challenge, timeout=2) as response:
        identity = json.load(response)
    # Bind the proof to the contacted port too: a stale listener cannot relay
    # another live Sharelet listener's challenge response and harvest the key.
    expected = local_identity_proof(key, root, challenge, role, urlsplit(base).port)
    if (not identity.get('ok') or identity.get('profile_id') != str(root)
            or not isinstance(identity.get('proof'), str) or not hmac.compare_digest(identity['proof'], expected)):
        raise ValueError('This listener did not prove its workspace identity')


def default_data_dir():
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Sharelet"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "sharelet"


class OwnerClient:
    def __init__(self, root=None, *, auto_start=True, role='owner'):
        self.root = Path(root or default_data_dir()).expanduser().resolve()
        self.auto_start = auto_start
        if role not in ('owner','agent'):
            raise ValueError('Unknown local client role')
        self.role = role

    def _connection(self):
        metadata = json.loads((self.root / "service.json").read_text(encoding="utf-8"))
        port = metadata.get("owner_port")
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError("Invalid local service metadata")
        key = (self.root / ("owner-v2.key" if self.role == 'owner' else 'agent.key')).read_text(encoding="utf-8").strip()
        base = "http://127.0.0.1:" + str(port)
        verify_local_service(base, self.root, key, self.role)
        with urlopen(Request(base + '/api/'+self.role+'/ping',
                             headers={"Authorization": "Bearer " + key}), timeout=2) as response:
            ping = json.load(response)
        if not ping.get("ok") or ping.get("profile_id") != str(self.root):
            raise ValueError("This listener belongs to another workspace")
        return base, key

    def ensure(self):
        try:
            self._connection()
            return
        except Exception:
            if not self.auto_start:
                raise CapsuleError("Start the Sharelet service for this workspace first.", "service_unavailable", 503) from None
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        launcher = ServiceLease(self.root / "launcher")
        owned = launcher.acquire()
        try:
            if owned:
                try:
                    self._connection()
                    return
                except Exception:
                    pass
                log_path = self.root / "service.log"
                fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "ab") as log:
                    flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
                    subprocess.Popen([sys.executable, str(Path(__file__).parent / "app.py"),
                                      "--mode", "local", "--data-dir", str(self.root)],
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                     start_new_session=os.name != "nt", creationflags=flags)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    self._connection()
                    return
                except Exception:
                    time.sleep(.1)
            raise CapsuleError("The local Sharelet service did not start. Inspect its private service log.",
                               "service_unavailable", 503)
        finally:
            if owned:
                launcher.release()

    def request(self, path, body=None):
        if not path.startswith('/api/'+self.role+'/') or "?" in path or "#" in path:
            raise ValueError("Unsupported creator request")
        self.ensure()
        base, key = self._connection()
        request = Request(base + path,
                          data=None if body is None else json.dumps(body, allow_nan=False).encode(),
                          headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as error:
            try:
                payload = json.load(error)
                message = payload.get("error", "The request failed.")
                code = payload.get("code", "owner_request_failed")
            except Exception:
                message, code = "The request failed.", "owner_request_failed"
            raise CapsuleError(message, code, error.code) from None
