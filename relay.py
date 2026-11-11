"""Stable share links through a Sharelet relay.

The host keeps a random relay identity. Links look like ``<relay>/h/<host>#<token>``:
the token is in the fragment, so the relay never receives it. While the quick
tunnel is live, the host reports its current origin; guests are forwarded there.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

DEFAULT_RELAY = "https://sharelet.109-123-230-235.sslip.io"
BEAT_SECONDS = 60
POLL_SECONDS = 3


def normalise_base(value):
    """Return an https relay base URL (http only for loopback tests), or '' when disabled."""
    if value in (None, "", False):
        return ""
    if not isinstance(value, str):
        raise ValueError("relay_url must be a URL or an empty string")
    parts = urlsplit(value.strip())
    loopback = parts.hostname in ("127.0.0.1", "localhost", "::1")
    if (parts.scheme != "https" and not (parts.scheme == "http" and loopback)) or not parts.hostname \
            or parts.query or parts.fragment or parts.username or parts.password or parts.path not in ("", "/"):
        raise ValueError("relay_url must be an https origin such as https://relay.example.com")
    return parts.scheme + "://" + parts.netloc


def _identity(root):
    path = Path(root) / "relay.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if re.fullmatch(r"[a-f0-9]{32}", value.get("id", "")) and re.fullmatch(r"[A-Za-z0-9_-]{43}", value.get("secret", "")):
            return value
    except (OSError, ValueError, AttributeError):
        pass
    value = {"id": secrets.token_hex(16), "secret": secrets.token_urlsafe(32)}
    temporary = path.with_name(path.name + "." + secrets.token_hex(4))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(value, output)
    try:
        # Never replace an identity another process created first.
        os.link(temporary, path)
    except FileExistsError:
        pass
    except OSError:
        if not path.exists():
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return json.loads(path.read_text(encoding="utf-8"))


class RelayLink:
    def __init__(self, root, base, *, public_link=None, beat_seconds=BEAT_SECONDS, poll_seconds=POLL_SECONDS):
        self.base = normalise_base(base)
        if not self.base:
            raise ValueError("A relay URL is required")
        identity = _identity(root)
        self.host_id, self._secret = identity["id"], identity["secret"]
        # Issue relay links only after this relay accepted the identity once, so an
        # unreachable or misconfigured relay never produces links that cannot open.
        self._marker = Path(root) / "relay-claimed.json"
        self.public_link = public_link
        self.beat_seconds, self.poll_seconds = beat_seconds, poll_seconds
        self._lock, self._stop = threading.Lock(), threading.Event()
        self._thread = None
        self._claimed = self._read_marker()
        self._reported, self._reported_at = None, 0.0
        self._info = {"state": "pending", "error": None}

    def _read_marker(self):
        try:
            value = json.loads(self._marker.read_text(encoding="utf-8"))
            return value.get("base") == self.base and value.get("host") == self.host_id
        except (OSError, ValueError, AttributeError):
            return False

    @property
    def ready(self):
        return self._claimed

    def link(self, token):
        return self.base + "/h/" + self.host_id + "#" + token

    def status(self):
        with self._lock:
            return {"base": self.base, "host": self.host_id, "ready": self._claimed, **self._info}

    def _post(self, path, body):
        request = Request(self.base + path, data=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json", "User-Agent": "Sharelet"}, method="POST")
        with urlopen(request, timeout=10) as response:
            return json.load(response)

    def _set(self, state, error=None):
        with self._lock:
            self._info = {"state": state, "error": error}

    def sync(self):
        """Claim the identity once, then report the current origin when it changes or is due."""
        state = self.public_link.status() if self.public_link else {"state": "offline", "origin": ""}
        origin = state["origin"] if state.get("state") == "live" else ""
        now = time.monotonic()
        if origin == self._reported and now - self._reported_at < (self.beat_seconds if origin else 600):
            return
        try:
            if not self._claimed:
                self._post("/api/hosts", {"id": self.host_id, "secret": self._secret})
                self._claimed = True
                try:
                    self._marker.write_text(json.dumps({"base": self.base, "host": self.host_id}), encoding="utf-8")
                except OSError:
                    pass
            self._post("/api/hosts/" + self.host_id + "/beat", {"secret": self._secret, "origin": origin})
            self._reported, self._reported_at = origin, now
            self._set("online" if origin else "waiting")
        except HTTPError as error:
            code = ""
            try:
                code = json.load(error).get("code", "")
            except Exception:
                pass
            if error.code == 404:
                self._claimed = False
            self._set("error", code or "relay_http_" + str(error.code))
            self._reported_at = now - self.beat_seconds + 10
        except Exception:
            self._set("error", "relay_unreachable")
            self._reported_at = now - self.beat_seconds + 15

    def start(self):
        if self._thread:
            return
        def run():
            while not self._stop.is_set():
                try:
                    self.sync()
                except Exception:
                    pass
                self._stop.wait(self.poll_seconds)
            # Tell the relay we went offline so guests see the offline page right away.
            try:
                if self._claimed and self._reported:
                    self._post("/api/hosts/" + self.host_id + "/beat", {"secret": self._secret, "origin": ""})
            except Exception:
                pass
        self._thread = threading.Thread(target=run, daemon=True, name="sharelet-relay")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=12)
            self._thread = None
