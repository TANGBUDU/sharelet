"""Regression tests for slot exhaustion, upload churn and never-sent attempts."""
import json
from pathlib import Path
import select
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import webserver
from host import HermesBridge
from file_inputs import FileStore
from jobs import ImageJobs
from media import MediaStore
from sharelet import CapsuleManager

SCHEMA = {"type": "object", "properties": {"text": {"type": "string", "maxLength": 50}},
          "required": ["text"], "additionalProperties": False}
PDF_SCHEMA = {"type": "object", "properties": {"files": {"type": "string", "maxLength": 500,
              "format": "sharelet-pdf-uploads"}}, "required": ["files"], "additionalProperties": False}
PDF = b"%PDF-1.4\n" + b"0" * 1000 + b"\n%%EOF\n"


class Harness(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.results = []
        self.manager = CapsuleManager(self.root, lambda s, t, a: self.results.pop(0) if self.results else {"ok": True, "result": a})
        self.jobs = ImageJobs(self.manager, MediaStore(self.root), files=FileStore(self.manager))
        self.server = webserver.create_server(self.manager, lambda: [], port=0, guest_only=True, jobs=self.jobs)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_port
        self.sockets = []

    def tearDown(self):
        for sock in self.sockets:
            sock.close()
        self.server.shutdown(); self.server.server_close()
        self.jobs.close()
        self.temp.cleanup()

    def share(self, schema=SCHEMA, **extra):
        return self.manager.create(title="t", server="srv", tool="tool", schema=schema, public_safe_approved=True, **extra)

    def request(self, path, body=None, raw=None, kind="application/json"):
        data = raw if raw is not None else json.dumps(body).encode()
        try:
            with urlopen(Request("http://127.0.0.1:%d%s" % (self.port, path), data=data,
                                 headers={"Content-Type": kind}), timeout=10) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def drip(self, path, kind, length, prefix):
        sock = socket.create_connection(("127.0.0.1", self.port))
        sock.sendall(("POST %s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Type: %s\r\nContent-Length: %d\r\n\r\n"
                      % (path, self.port, kind, length)).encode() + prefix)
        self.sockets.append(sock)
        return sock


class SlowBodyTests(Harness):
    def test_slow_uploads_do_not_block_other_links(self):
        pdf_links = [self.share(PDF_SCHEMA) for _ in range(4)]
        other = self.share()
        for token in pdf_links:
            for _ in range(2):
                self.drip("/api/s/%s/uploads" % token, "application/pdf", 5_000_000, b"%PDF-")
        time.sleep(.5)
        status, body = self.request("/api/s/%s/jobs" % other, {"input": {"text": "hi"}, "request_id": "a" * 32})
        self.assertEqual(status, 202, body)

    def test_slow_json_bodies_do_not_hold_processing_slots(self):
        token = self.share()
        for _ in range(64):
            self.drip("/api/s/%s/jobs" % token, "application/json", 200, b"{")
        time.sleep(.5)
        readable, _, _ = select.select(self.sockets, [], [], 1)
        self.assertEqual(len(readable), 64-webserver.JSON_PER_LINK)
        self.assertTrue(all(b' 429 ' in sock.recv(4096).split(b'\r\n', 1)[0] for sock in readable))
        other = self.share()
        with urlopen("http://127.0.0.1:%d/s/%s" % (self.port, other), timeout=3) as response:
            self.assertEqual(response.status, 200)
        status, body = self.request("/api/s/%s/jobs" % other, {"input": {"text": "hi"}, "request_id": "b" * 32})
        self.assertEqual(status, 202, body)

    def test_global_json_body_limit_keeps_pages_available(self):
        for _ in range(webserver.JSON_SLOTS):
            self.drip('/api/s/%s/jobs' % self.share(), 'application/json', 200, b'{')
        time.sleep(.3)
        token = self.share()
        status, body = self.request('/api/s/%s/jobs' % token, {'input': {'text': 'hi'}, 'request_id': 'f'*32})
        self.assertEqual((status, body['code']), (503, 'busy'))
        with urlopen('http://127.0.0.1:%d/s/%s' % (self.port, token), timeout=3) as response:
            self.assertEqual(response.status, 200)

    def test_json_reader_slots_are_released_after_timeout_and_disconnect(self):
        token = self.share()
        with patch.object(webserver, 'JSON_BODY_SECONDS', .3):
            sock = self.drip('/api/s/%s/jobs' % token, 'application/json', 200, b'{')
            sock.settimeout(3)
            self.assertIn(b' 408 ', sock.recv(4096).split(b'\r\n', 1)[0])
        disconnected = self.drip('/api/s/%s/jobs' % token, 'application/json', 200, b'{')
        disconnected.shutdown(socket.SHUT_RDWR)
        disconnected.close()
        time.sleep(.1)
        status, body = self.request('/api/s/%s/jobs' % token, {'input': {'text': 'hi'}, 'request_id': 'g'*32})
        self.assertEqual(status, 202, body)

    def test_upload_body_has_an_overall_deadline(self):
        token = self.share(PDF_SCHEMA)
        with patch.object(webserver, "UPLOAD_BODY_SECONDS", 1):
            sock = self.drip("/api/s/%s/uploads" % token, "application/pdf", 5000, b"%PDF-")
            started, reply = time.monotonic(), ""
            # Keep dripping a byte well inside the old per-read timeout until the server answers.
            while time.monotonic()-started < 5:
                readable, _, _ = select.select([sock], [], [], .3)
                if readable:
                    reply = sock.recv(4096).decode(errors="replace")
                    break
                sock.sendall(b"0")
        self.assertIn(" 408 ", reply.splitlines()[0])
        self.assertLess(time.monotonic()-started, 3)

    def test_one_link_cannot_take_every_upload_slot(self):
        token = self.share(PDF_SCHEMA)
        for _ in range(2):
            self.drip("/api/s/%s/uploads" % token, "application/pdf", 5_000_000, b"%PDF-")
        time.sleep(.3)
        status, body = self.request("/api/s/%s/uploads" % token, raw=PDF, kind="application/pdf")
        self.assertEqual((status, body["code"]), (429, "busy"))
        status, body = self.request("/api/s/%s/uploads" % self.share(PDF_SCHEMA), raw=PDF, kind="application/pdf")
        self.assertEqual(status, 201, body)


class UploadAllowanceTests(Harness):
    def test_upload_churn_is_rate_limited_per_link(self):
        token = self.share(PDF_SCHEMA)
        codes = []
        # Uploads and removals share one per-link allowance.
        for _ in range(webserver.UPLOAD_PER_LINK_MINUTE // 2):
            status, body = self.request("/api/s/%s/uploads" % token, raw=PDF, kind="application/pdf")
            codes.append(status)
            if status == 201:
                codes.append(self.request("/api/s/%s/uploads/%s/remove" % (token, body["upload"]["id"]), {})[0])
        self.assertTrue(set(codes) <= {200, 201}, codes)
        self.assertIn(429, [self.request("/api/s/%s/uploads" % token, raw=PDF, kind="application/pdf")[0] for _ in range(20)])
        # Another link and the run allowance are unaffected.
        self.assertEqual(self.request("/api/s/%s/uploads" % self.share(PDF_SCHEMA), raw=PDF, kind="application/pdf")[0], 201)
        self.assertEqual(self.manager.public_info(token)["calls"], 0)


class NeverSentTests(Harness):
    def wait(self, token, job_id):
        for _ in range(100):
            job = self.jobs.get(token, job_id)
            if job["state"] not in ("queued", "running"):
                return job
            time.sleep(.05)
        self.fail("job did not finish")

    def test_never_sent_attempt_is_refunded(self):
        token = self.share(max_calls=3)
        self.results.append({"ok": False, "not_sent": True})
        job = self.jobs.submit(token, {"text": "x"}, "c" * 32)
        job = self.wait(token, job["id"])
        self.assertEqual(job["state"], "failed")
        self.assertIn("no run was charged", job["error"])
        self.assertEqual(self.manager.public_info(token)["calls"], 0)
        self.assertEqual(self.jobs.usage()["used"], 0)
        # A repeated request key returns the same finished job; it is not refunded twice.
        self.assertEqual(self.jobs.submit(token, {"text": "x"}, "c" * 32)["id"], job["id"])
        self.assertEqual(self.jobs.usage()["used"], 0)

    def test_ordinary_failure_is_still_charged(self):
        token = self.share(max_calls=3)
        self.results.append({"ok": False})
        job = self.wait(token, self.jobs.submit(token, {"text": "x"}, "d" * 32)["id"])
        self.assertEqual(job["state"], "failed")
        self.assertEqual(self.manager.public_info(token)["calls"], 1)
        self.assertEqual(self.jobs.usage()["used"], 1)

    def test_never_sent_image_refunds_link_and_both_workspace_budgets(self):
        token = self.share(kind='image', preset='clay')
        self.results.append({'ok': False, 'not_sent': True})
        job = self.wait(token, self.jobs.submit(token, {'text': 'no provider call'}, 'i'*32)['id'])
        self.assertEqual(job['state'], 'failed')
        self.assertEqual(self.manager.public_info(token)['calls'], 0)
        self.assertEqual(self.jobs.usage()['used'], 0)
        self.assertEqual(self.jobs.budget()['used'], 0)


class HermesSlotTests(unittest.TestCase):
    def test_full_slots_report_not_sent_and_recover(self):
        gate = threading.Event()
        dispatched = []
        class Ctx:
            plugin_id = "sharelet"
            def get_config(self, key, default=None): return default
            def dispatch_tool(self, name, args):
                dispatched.append(args); gate.wait(10); return json.dumps({"value": 1})
        inventory = [{"name": "mcp__files__slow", "toolset": "mcp-files", "schema": {"parameters": SCHEMA}}]
        config = {"plugins": {"entries": {"sharelet": {"mcp_allowlist": ["files"]}}}, "mcp_servers": {"files": {}}}
        bridge = HermesBridge(Ctx(), inventory=lambda: inventory, configuration=lambda: config, call_timeout=.3)
        hung = [bridge.call("files", "mcp__files__slow", {"text": "x"}) for _ in range(4)]
        self.assertEqual(hung, [{"ok": False, "uncertain": True}] * 4)
        self.assertEqual(bridge.call("files", "mcp__files__slow", {"text": "y"}), {"ok": False, "not_sent": True})
        self.assertEqual(len(dispatched), 4)
        gate.set(); time.sleep(.2)
        self.assertEqual(bridge.call("files", "mcp__files__slow", {"text": "z"}), {"ok": True, "result": {"value": 1}})

    def test_slot_freed_too_close_to_deadline_is_not_sent(self):
        dispatched = []
        class Ctx:
            plugin_id = "sharelet"
            def get_config(self, key, default=None): return default
            def dispatch_tool(self, name, args):
                dispatched.append(args); return json.dumps({"value": 1})
        inventory = [{"name": "mcp__files__slow", "toolset": "mcp-files", "schema": {"parameters": SCHEMA}}]
        config = {"plugins": {"entries": {"sharelet": {"mcp_allowlist": ["files"]}}}, "mcp_servers": {"files": {}}}
        bridge = HermesBridge(Ctx(), inventory=lambda: inventory, configuration=lambda: config, call_timeout=2)
        for _ in range(4):
            bridge._rpc_slots.acquire()
        # A slot frees up only 1.8 s into the 2 s deadline.
        threading.Timer(1.8, bridge._rpc_slots.release).start()
        self.assertEqual(bridge.call("files", "mcp__files__slow", {"text": "late"}), {"ok": False, "not_sent": True})
        self.assertEqual(dispatched, [])
        # The slot was handed back, so the next call still has room to run.
        for _ in range(3):
            bridge._rpc_slots.release()
        self.assertEqual(bridge.call("files", "mcp__files__slow", {"text": "now"}), {"ok": True, "result": {"value": 1}})
        self.assertEqual(dispatched, [{"text": "now"}])


class ReplyTests(Harness):
    def test_client_disconnect_during_reply_is_quiet(self):
        import contextlib, io, struct, sys
        token = self.share()
        errors = io.StringIO()
        # A zero linger timeout makes close() send a reset, as an abruptly closed browser tab can.
        linger = struct.pack("HH" if sys.platform == "win32" else "ii", 1, 0)
        with contextlib.redirect_stderr(errors):
            for _ in range(20):
                sock = socket.create_connection(("127.0.0.1", self.port))
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
                sock.sendall(("GET /s/%s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n" % (token, self.port)).encode())
                sock.close()
            time.sleep(.5)
        self.assertNotIn("Traceback", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
