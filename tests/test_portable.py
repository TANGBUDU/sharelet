"""Real stdio and Streamable HTTP MCP checks. No paid model calls."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen
from connections import PortableBridge
from jobs import ImageJobs
from media import MediaStore
from media import unwrap_result
from sharelet import CapsuleError, CapsuleManager

ROOT = Path(__file__).resolve().parents[1]


class PortableTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bridge = PortableBridge(self.root, builtin=False, images=False)
        self.children = []

    def tearDown(self):
        self.bridge.close()
        for child in self.children:
            child.terminate()
            try: child.wait(timeout=5)
            except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=3)
        self.temp.cleanup()

    def configuration(self, servers):
        path = self.root/"source.json"
        path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
        return path

    def ready(self, name):
        until = time.monotonic()+20
        while time.monotonic() < until:
            self.bridge.catalog()
            state = next((row["status"] for row in self.bridge.connection_status()["servers"] if row["name"] == name), None)
            if state == "ready": return
            if state == "unavailable": self.fail("Controlled MCP connection unavailable")
            time.sleep(.03)
        self.fail("Controlled MCP connection did not become ready")

    def stdio(self, **extra):
        return {"command": sys.executable, "args": [str(ROOT/"tests/fixture_mcp.py")], **extra}

    def test_stdio_discovery_execution_and_unsupported_tool_reasons(self):
        source = self.configuration({"fixture": self.stdio()})
        self.bridge.save_connections({"path": str(source), "servers": ["fixture"]})
        self.ready("fixture")
        entries = {row["tool"]: row for row in self.bridge.catalog()}
        self.assertTrue(entries["transform"]["supported"])
        self.assertFalse(entries["nested"]["supported"])
        self.assertFalse(entries["destructive"]["supported"])
        self.assertTrue(entries['optional_limit']['supported'])
        nullable=self.bridge.call('fixture','optional_limit',{'text':'hello','limit':None})
        self.assertEqual(unwrap_result(nullable.get('structuredContent',nullable['result']))['limit'],None)
        response = self.bridge.call("fixture", "transform", {"text": "hello", "enabled": False, "factor": 2, "mode": "upper"})
        self.assertTrue(response["ok"])
        self.assertEqual(unwrap_result(response.get("structuredContent", response["result"])),
                         {"output": "HELLO", "weighted_length": 10.0, "enabled": False})

    def test_configuration_reference_never_copies_credentials_or_returns_them_in_status(self):
        secret = secrets.token_urlsafe(32)
        source = self.configuration({"fixture": self.stdio(env={"FIXTURE_UNUSED_SECRET": secret})})
        self.assertNotIn(secret, json.dumps(self.bridge.inspect_config({"path": str(source)})))
        self.bridge.save_connections({"path": str(source), "servers": ["fixture"]})
        self.ready("fixture")
        self.assertNotIn(secret, self.bridge.path.read_text())
        self.assertNotIn(secret, json.dumps(self.bridge.connection_status()))

    def test_removing_an_imported_server_revokes_its_connection(self):
        source = self.configuration({"fixture": self.stdio()})
        self.bridge.save_connections({"path": str(source), "servers": ["fixture"]})
        self.ready("fixture")
        self.bridge.save_connections({"path": str(source), "servers": []})
        self.assertEqual(self.bridge.connection_status()["servers"], [])
        self.assertFalse(self.bridge.call("fixture", "transform", {"text": "hello"})["ok"])

    def test_reserved_names_cannot_shadow_builtin_dispatch(self):
        source = self.configuration({"sharelet_public": self.stdio()})
        self.assertFalse(self.bridge.inspect_config({"path": str(source)})['servers'][0]['supported'])
        with self.assertRaises(CapsuleError):
            self.bridge.save_connections({"path": str(source), "servers": ['sharelet_public']})

    def test_revoked_request_in_real_mcp_queue_is_not_sent_or_charged(self):
        marker = self.root/'executions.txt'
        source = self.configuration({'fixture': self.stdio(env={'SHARELET_TEST_MARKER': str(marker)})})
        self.bridge.save_connections({'path': str(source), 'servers': ['fixture']})
        self.ready('fixture')
        first = []
        worker = threading.Thread(target=lambda: first.append(self.bridge.call('fixture', 'transform', {'text': 'first', 'delay_ms': 3500})))
        worker.start()
        manager = CapsuleManager(self.root/'jobs', self.bridge.call)
        jobs = ImageJobs(manager, MediaStore(manager.root))
        try:
            until = time.monotonic()+5
            while not marker.exists() and time.monotonic() < until: time.sleep(.01)
            self.assertTrue(marker.exists(), 'The first real MCP call did not start')
            entry = next(row for row in self.bridge.catalog() if row['tool'] == 'transform')
            token = manager.create(title='Queued MCP call', server='fixture', tool='transform', schema=entry['schema'], public_safe_approved=True)
            job = jobs.submit(token, {'text': 'must not execute'}, 'q'*32)
            queue = self.bridge._connections['fixture']['queue']
            until = time.monotonic()+2
            while queue.qsize() == 0 and time.monotonic() < until: time.sleep(.01)
            self.assertEqual(queue.qsize(), 1, 'The second call must be waiting inside the MCP connection')
            manager.revoke(token)
            worker.join(timeout=8)
            self.assertFalse(worker.is_alive())
            jobs.executor.shutdown(wait=True)
            self.assertTrue(first[0]['ok'])
            self.assertEqual(marker.read_text().splitlines(), ['execution'])
            with manager._db() as db:
                self.assertEqual(db.execute('SELECT state FROM image_jobs WHERE id=?', (job['id'],)).fetchone()[0], 'interrupted')
            self.assertEqual(jobs.usage()['used'], 0)
            self.assertEqual(manager.public_info(token)['calls'], 0)
            self.assertTrue(self.bridge.call('fixture', 'transform', {'text': 'still works'})['ok'])
            self.assertEqual(len(marker.read_text().splitlines()), 2)
        finally:
            worker.join(timeout=8)
            jobs.executor.shutdown(wait=True, cancel_futures=True)

    def test_streamable_http_authentication_and_tool_call(self):
        secret = secrets.token_urlsafe(32)
        metadata = self.root/"http.json"
        child = subprocess.Popen([sys.executable, str(ROOT/"tests/fixture_mcp.py"), "--http-metadata", str(metadata)],
                                 env={**os.environ, "SHARELET_TEST_TOKEN": secret}, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.children.append(child)
        for _ in range(200):
            if metadata.is_file():
                port = json.loads(metadata.read_text())["port"]
                try: urlopen("http://127.0.0.1:"+str(port)+"/mcp", timeout=.2)
                except HTTPError as error:
                    if error.code == 401: break
                except OSError: pass
            time.sleep(.03)
        else: self.fail("Controlled HTTP fixture did not start")
        source = self.configuration({"http_fixture": {"url": "http://127.0.0.1:"+str(port)+"/mcp",
                                                     "headers": {"Authorization": "Bearer $" + "{SHARELET_HTTP_TEST_TOKEN}"}}})
        with patch.dict(os.environ, {"SHARELET_HTTP_TEST_TOKEN": secret}):
            self.bridge.save_connections({"path": str(source), "servers": ["http_fixture"]})
            self.ready("http_fixture")
            result = self.bridge.call("http_fixture", "transform", {"text": "http works"})
            self.assertTrue(result["ok"])
            self.assertNotIn(secret, json.dumps(self.bridge.connection_status()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
