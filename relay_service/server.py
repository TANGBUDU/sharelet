"""Sharelet link relay: a stable address that forwards guests to a host's current tunnel.

The relay only stores, per host, a random id, a hash of the host's secret and the
host's current public origin. Share tokens travel in the URL fragment, which
browsers never send to a server, so the relay cannot see or use any share link.

Run behind a TLS-terminating reverse proxy that sets X-Real-IP:
    python server.py --db /var/lib/sharelet-relay/relay.sqlite3 --port 9580
"""
from __future__ import annotations

import argparse
from collections import defaultdict, deque
import hashlib
import hmac
import json
import re
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

HOST_ID = re.compile(r"[a-f0-9]{32}")
SECRET = re.compile(r"[A-Za-z0-9_-]{43}")
QUICK_TUNNEL = re.compile(r"https://[a-z0-9-]{1,80}\.trycloudflare\.com")
ONLINE_SECONDS = 150
MAX_HOSTS = 50000
MAX_BODY = 1024
WEB = Path(__file__).with_name("web")
ASSETS = {"/relay.js": ("relay.js", "text/javascript; charset=utf-8"),
          "/relay.css": ("relay.css", "text/css; charset=utf-8"),
          "/icon.svg": ("icon.svg", "image/svg+xml")}


class Window:
    """Sliding-window counters keyed by client address or host id."""
    def __init__(self, limit, seconds):
        self.limit, self.seconds = limit, seconds
        self.hits, self.lock = defaultdict(deque), threading.Lock()

    def admit(self, key):
        now = time.monotonic()
        with self.lock:
            hits = self.hits[key]
            while hits and hits[0] <= now - self.seconds:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            if len(self.hits) > 100000:
                # Drop idle keys rather than grow without bound.
                for stale in [k for k, v in self.hits.items() if not v][:50000]:
                    del self.hits[stale]
            return True


def secret_hash(secret):
    return hashlib.sha256(("sharelet-relay-v1\0" + secret).encode()).hexdigest()


def verify_origin(origin, host_id, timeout=6):
    """Only point at a live Sharelet guest listener that reports this host id."""
    request = Request(origin + "/healthz", headers={"User-Agent": "Sharelet-Relay/1"})
    with urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            return False
        info = json.loads(response.read(4096))
    return info.get("ok") is True and info.get("guest_only") is True and info.get("relay_host") == host_id


class Relay:
    def __init__(self, path, *, clock=time.time, verify=verify_origin):
        self.path, self.clock, self.verify = Path(path), clock, verify
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS hosts (
                id TEXT PRIMARY KEY, secret_hash TEXT NOT NULL, origin TEXT NOT NULL DEFAULT '',
                seen REAL NOT NULL DEFAULT 0, created REAL NOT NULL);
            """)

    def db(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return _Transaction(connection)

    def claim(self, host_id, secret):
        if not HOST_ID.fullmatch(host_id or "") or not SECRET.fullmatch(secret or ""):
            raise RelayError(400, "invalid_identity")
        digest = secret_hash(secret)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT secret_hash FROM hosts WHERE id=?", (host_id,)).fetchone()
            if row:
                if not hmac.compare_digest(row["secret_hash"], digest):
                    raise RelayError(409, "id_taken")
                return
            if db.execute("SELECT COUNT(*) FROM hosts").fetchone()[0] >= MAX_HOSTS:
                raise RelayError(503, "relay_full")
            db.execute("INSERT INTO hosts (id, secret_hash, created) VALUES (?,?,?)", (host_id, digest, self.clock()))

    def beat(self, host_id, secret, origin):
        if not HOST_ID.fullmatch(host_id or "") or not SECRET.fullmatch(secret or ""):
            raise RelayError(400, "invalid_identity")
        if origin and not QUICK_TUNNEL.fullmatch(origin):
            raise RelayError(400, "invalid_origin")
        with self.db() as db:
            row = db.execute("SELECT secret_hash, origin FROM hosts WHERE id=?", (host_id,)).fetchone()
        if not row:
            raise RelayError(404, "unknown_host")
        if not hmac.compare_digest(row["secret_hash"], secret_hash(secret)):
            raise RelayError(403, "wrong_secret")
        if origin and origin != row["origin"]:
            try:
                ok = self.verify(origin, host_id)
            except Exception:
                ok = False
            if not ok:
                raise RelayError(422, "origin_not_verified")
        with self.db() as db:
            db.execute("UPDATE hosts SET origin=?, seen=? WHERE id=?", (origin, self.clock() if origin else 0, host_id))

    def lookup(self, host_id):
        if not HOST_ID.fullmatch(host_id or ""):
            raise RelayError(404, "unknown_host")
        with self.db() as db:
            row = db.execute("SELECT origin, seen FROM hosts WHERE id=?", (host_id,)).fetchone()
        if not row:
            raise RelayError(404, "unknown_host")
        online = bool(row["origin"]) and self.clock() - row["seen"] <= ONLINE_SECONDS
        return {"online": online, "origin": row["origin"] if online else None}

    def prune(self, idle_days=60):
        """Forget hosts that have not reported for a long time."""
        cutoff = self.clock() - idle_days * 86400
        with self.db() as db:
            db.execute("DELETE FROM hosts WHERE seen < ? AND created < ?", (cutoff, cutoff))


class _Transaction:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, kind, value, trace):
        try:
            if kind is None:
                self.connection.commit()
            else:
                self.connection.rollback()
        finally:
            self.connection.close()


class RelayError(Exception):
    def __init__(self, status, code):
        super().__init__(code)
        self.status, self.code = status, code


def create_server(relay, *, port=9580, host="127.0.0.1", trust_proxy=True):
    claims = Window(20, 3600)        # per client address
    beats = Window(12, 60)           # per host id
    reads = Window(240, 60)          # per client address

    class Handler(BaseHTTPRequestHandler):
        server_version = "Sharelet-Relay/1"

        def setup(self):
            super().setup(); self.connection.settimeout(15)

        def log_message(self, *args):
            pass

        def client(self):
            if trust_proxy:
                return self.headers.get("X-Real-IP") or self.client_address[0]
            return self.client_address[0]

        def reply(self, status, body, kind="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", kind); self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store"); self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff"); self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; "
                             "img-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
            self.end_headers()
            try:
                self.wfile.write(data)
            except ConnectionError:
                self.close_connection = True

        def do_GET(self):
            path = urlsplit(self.path).path
            try:
                if path == "/healthz":
                    return self.reply(200, {"ok": True, "service": "sharelet-relay"})
                if path in ASSETS:
                    name, kind = ASSETS[path]
                    return self.reply(200, (WEB / name).read_bytes(), kind)
                if not reads.admit(self.client()):
                    raise RelayError(429, "rate_limited")
                match = re.fullmatch(r"/h/([a-f0-9]{32})/?", path)
                if match:
                    return self.reply(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
                match = re.fullmatch(r"/api/hosts/([a-f0-9]{32})", path)
                if match:
                    return self.reply(200, {"ok": True, **relay.lookup(match[1])})
                if path == "/":
                    return self.reply(200, (WEB / "about.html").read_bytes(), "text/html; charset=utf-8")
                raise RelayError(404, "not_found")
            except RelayError as error:
                self.reply(error.status, {"ok": False, "code": error.code})
            except Exception:
                self.reply(500, {"ok": False, "code": "server_error"})

        def do_POST(self):
            path = urlsplit(self.path).path
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if self.headers.get("Transfer-Encoding") or not 1 <= length <= MAX_BODY:
                    raise RelayError(400, "invalid_body")
                if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                    raise RelayError(415, "json_required")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise RelayError(400, "invalid_body")
                if path == "/api/hosts":
                    if set(body) != {"id", "secret"}:
                        raise RelayError(400, "invalid_body")
                    if not claims.admit(self.client()):
                        raise RelayError(429, "rate_limited")
                    relay.claim(body["id"], body["secret"])
                    return self.reply(200, {"ok": True})
                match = re.fullmatch(r"/api/hosts/([a-f0-9]{32})/beat", path)
                if match:
                    if set(body) != {"secret", "origin"} or not isinstance(body["origin"], str):
                        raise RelayError(400, "invalid_body")
                    if not beats.admit(match[1]):
                        raise RelayError(429, "rate_limited")
                    relay.beat(match[1], body["secret"], body["origin"])
                    return self.reply(200, {"ok": True})
                raise RelayError(404, "not_found")
            except RelayError as error:
                self.reply(error.status, {"ok": False, "code": error.code})
            except (ValueError, TypeError):
                self.reply(400, {"ok": False, "code": "invalid_body"})
            except Exception:
                self.reply(500, {"ok": False, "code": "server_error"})

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default="relay.sqlite3")
    parser.add_argument("--port", type=int, default=9580)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    relay = Relay(args.db)
    server = create_server(relay, port=args.port, host=args.host)
    def housekeeping():
        while True:
            time.sleep(86400)
            try:
                relay.prune()
            except Exception:
                pass
    threading.Thread(target=housekeeping, daemon=True).start()
    print("Sharelet relay listening on %s:%d" % (args.host, server.server_port), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
