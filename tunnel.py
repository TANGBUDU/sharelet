"""One-click, owned guest-only Cloudflare preview. No shell or arbitrary argv."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import secrets
import subprocess
import threading
import time
from pathlib import Path
from urllib.request import Request, urlopen

CONNECT_TIMEOUT = 180
HEALTH_INTERVAL = 30
HEALTH_FAILURES = 2
RETRY_DELAYS = (5, 15, 30, 60, 120, 300)
CLOUDFLARED_VERSION = "2026.10.7"
# Release-owned pins: never obtain a checksum from runtime release metadata.
# Upstream has no Windows ARM64 binary. Windows 11 ARM64 uses its x64 emulator.
CLOUDFLARED_ASSETS = {
    ("linux", "amd64"): ("cloudflared-linux-amd64", "1f99fb518b83dec2ba8d870a7cc991d82a77e6196295a3d2754ba17ab9f91872"),
    ("linux", "arm64"): ("cloudflared-linux-arm64", "3df43aedc9ff291d1735b5cb576cb97e0b157488796db2e32b28aecabb445775"),
    ("windows", "amd64"): ("cloudflared-windows-amd64.exe", "2493dbc168923bb3e59e5e7716f71deced92992eea9acd6e9d74f84ac7751343"),
    ("windows", "arm64"): ("cloudflared-windows-amd64.exe", "2493dbc168923bb3e59e5e7716f71deced92992eea9acd6e9d74f84ac7751343"),
}
MAX_CONNECTOR = 120 * 1024 * 1024


def connector_asset():
    system, machine = platform.system().lower(), platform.machine().lower()
    architecture = "arm64" if machine in ("arm64", "aarch64") else "amd64" if machine in ("x86_64", "amd64") else None
    if (system, architecture) not in CLOUDFLARED_ASSETS:
        raise RuntimeError("Automatic connector installation supports Linux and Windows. Set a trusted cloudflared_path on this platform.")
    if system == "windows" and architecture == "arm64":
        try:
            build = int(platform.version().rsplit('.', 1)[-1])
        except ValueError:
            build = 0
        if build < 22000:
            raise RuntimeError("Automatic installation on Windows ARM64 requires Windows 11 x64 emulation. Set a trusted cloudflared_path instead.")
    return CLOUDFLARED_ASSETS[(system, architecture)]


class PublicLink:
    def __init__(self, root, *, binary=None):
        self.root = Path(root)
        self.bin = Path(binary) if binary else self.root / "bin" / ("cloudflared.exe" if os.name == "nt" else "cloudflared")
        self._managed = binary is None
        self.guest_port = None
        self._lock, self._epoch = threading.RLock(), 0
        self._process = None
        self._deadline = None
        self._health_stop = threading.Event()
        self._health_epoch = None
        self._info = {"state": "offline", "origin": "", "error": None}
        # While wanted, a dropped connection is retried automatically with backoff.
        self._wanted = False
        self._retry = None
        self._failures = 0

    @property
    def wanted(self):
        return self._wanted

    def status(self):
        with self._lock:
            if self._process and self._process.poll() is not None and self._info["state"] == "live":
                self._fail(self._epoch, "The public connector stopped. Reconnecting automatically.")
            return dict(self._info)

    @property
    def origin(self):
        return self.status()["origin"]

    def start(self):
        with self._lock:
            self._wanted = True
            if self._retry:
                self._retry.cancel(); self._retry = None
            if self._info["state"] in ("installing", "connecting", "checking", "live"):
                return dict(self._info)
            if not self.guest_port:
                raise RuntimeError("Guest service is not ready")
            self._epoch += 1
            epoch = self._epoch
            self._health_stop.set()
            self._health_stop = threading.Event()
            self._info = {"state": "installing" if not self.bin.exists() else "connecting", "origin": "", "error": None}
            self._deadline = threading.Timer(CONNECT_TIMEOUT, self._timed_out, args=(epoch,))
            self._deadline.daemon = True
            self._deadline.start()
            threading.Thread(target=self._open, args=(epoch,), daemon=True, name="sharelet-public-link").start()
            return dict(self._info)

    def _timed_out(self, epoch):
        with self._lock:
            if epoch != self._epoch or self._info["state"] in ("live", "offline", "failed"):
                return
            self._fail(epoch, "The public connector timed out. Reconnecting automatically.")

    def _verified_cache(self):
        if not self.bin.is_file() or self.bin.stat().st_size > MAX_CONNECTOR:
            return False
        _, expected = connector_asset()
        digest = hashlib.sha256()
        with self.bin.open('rb') as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest() == expected

    def _install(self):
        name, expected = connector_asset()
        url = "https://github.com/cloudflare/cloudflared/releases/download/" + CLOUDFLARED_VERSION + "/" + name
        self.bin.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.bin.with_name(self.bin.name+".download-"+secrets.token_hex(6))
        digest = hashlib.sha256()
        size = 0
        try:
            with urlopen(Request(url, headers={"User-Agent": "Sharelet/0.4"}), timeout=40) as response, temporary.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_CONNECTOR:
                        raise RuntimeError("Connector download exceeded its limit")
                    digest.update(chunk); output.write(chunk)
            if digest.hexdigest() != expected:
                raise RuntimeError("Connector checksum verification failed")
            temporary.chmod(0o700)
            os.replace(temporary, self.bin)
        finally:
            temporary.unlink(missing_ok=True)

    def _open(self, epoch):
        try:
            if not self.bin.exists() or self._managed and not self._verified_cache():
                self._install()
            with self._lock:
                if epoch != self._epoch:
                    return
                self._info["state"] = "connecting"
            config = self.root / "tunnel-preview.yml"
            config.write_text("{}\n", encoding="utf-8")
            env = {key: value for key, value in os.environ.items() if key.upper() in {
                "PATH", "SYSTEMROOT", "WINDIR", "TMP", "TEMP", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
                "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"}}
            process = subprocess.Popen([str(self.bin), "tunnel", "--config", str(config), "--no-autoupdate", "--protocol", "http2",
                                        "--url", "http://127.0.0.1:" + str(self.guest_port)],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, encoding="utf-8", errors="replace", env=env,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            with self._lock:
                if epoch != self._epoch:
                    process.terminate(); return
                self._process = process
            started = time.monotonic()
            for line in process.stdout:
                match = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
                if match:
                    origin = match[0]
                    with self._lock:
                        if epoch != self._epoch:
                            return
                        self._info.update(state="checking", origin=origin)
                    threading.Thread(target=self._check, args=(epoch, origin), daemon=True, name="sharelet-public-check").start()
                if time.monotonic() - started > 90 and not self.origin:
                    raise RuntimeError("The public connector did not return an address")
                with self._lock:
                    if epoch != self._epoch:
                        return
            with self._lock:
                if epoch == self._epoch:
                    self._fail(epoch, "The public connector stopped. Reconnecting automatically.")
        except Exception:
            self._fail(epoch,"Could not open a public link. Check network access and try again.")

    def _check(self, epoch, origin):
        deadline=time.monotonic()+110
        while time.monotonic()<deadline:
            with self._lock:
                if epoch != self._epoch:
                    return
            try:
                with urlopen(origin + "/healthz", timeout=3) as response:
                    info = json.load(response)
                if info.get("ok") and info.get("guest_only") is True:
                    with self._lock:
                        if epoch == self._epoch:
                            self._info.update(state="live", error=None)
                            self._failures = 0
                            if self._deadline:
                                self._deadline.cancel()
                            if self._health_epoch != epoch:
                                self._health_epoch = epoch
                                threading.Thread(target=self._monitor_live,args=(epoch,origin,self._health_stop),
                                                 daemon=True,name='sharelet-public-health').start()
                    return
            except Exception:
                pass
            time.sleep(2)
        self._fail(epoch,"The public address could not be reached. Reconnecting automatically.")

    def _monitor_live(self, epoch, origin, stop):
        failures = 0
        while not stop.wait(HEALTH_INTERVAL):
            with self._lock:
                if epoch != self._epoch or self._info['state'] != 'live':
                    return
            try:
                with urlopen(origin+'/healthz',timeout=3) as response:
                    value=json.load(response)
                if not value.get('ok') or value.get('guest_only') is not True:
                    raise ValueError('Unexpected guest response')
                failures=0
            except Exception:
                failures+=1
                if failures>=HEALTH_FAILURES:
                    self._fail(epoch,'The public address stopped responding. Reconnecting automatically.')
                    return

    def _fail(self,epoch,message):
        with self._lock:
            if epoch!=self._epoch:
                return
            self._epoch+=1
            self._health_stop.set()
            if self._deadline:
                self._deadline.cancel()
            process,self._process=self._process,None
            self._info={"state":"failed","origin":"","error":message}
            if self._wanted:
                delay=RETRY_DELAYS[min(self._failures,len(RETRY_DELAYS)-1)]
                self._failures+=1
                self._info["retry_in"]=delay
                if self._retry:
                    self._retry.cancel()
                self._retry=threading.Timer(delay,self._retry_start)
                self._retry.daemon=True
                self._retry.start()
        if process and process.poll() is None:
            process.terminate()
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.kill()

    def _retry_start(self):
        with self._lock:
            if not self._wanted or self._info["state"] != "failed":
                return
            self._retry = None
        try:
            self.start()
        except Exception:
            pass

    def stop(self):
        with self._lock:
            self._wanted = False
            self._failures = 0
            if self._retry:
                self._retry.cancel(); self._retry = None
            self._epoch += 1
            self._health_stop.set()
            if self._deadline:
                self._deadline.cancel()
            process, self._process = self._process, None
            self._info = {"state": "offline", "origin": "", "error": None}
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=3)
        return self.status()
