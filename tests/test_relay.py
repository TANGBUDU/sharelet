"""Stable links: the relay service, the host's relay client and on-demand public access."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from relay import RelayLink, normalise_base
from relay_service.server import ONLINE_SECONDS, Relay, RelayError, create_server as relay_server
from sharelet import CapsuleManager
import tunnel
from tunnel import PublicLink
from webserver import create_server

ORIGIN = "https://quiet-bird-example.trycloudflare.com"
OTHER = "https://loud-fish-example.trycloudflare.com"
TOKEN_SCHEMA = {"type": "object", "properties": {"text": {"type": "string", "maxLength": 50}},
                "required": ["text"], "additionalProperties": False}


class FakePublic:
    def __init__(self):
        self.info = {"state": "offline", "origin": "", "error": None}
        self.wanted, self.starts = False, 0

    def status(self):
        return dict(self.info)

    @property
    def origin(self):
        return self.info["origin"] if self.info["state"] == "live" else ""

    def start(self):
        self.wanted, self.starts = True, self.starts + 1

    def stop(self):
        self.wanted = False


class RelayStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clock = [1000.0]
        self.verified = []
        self.relay = Relay(Path(self.temp.name) / "relay.sqlite3", clock=lambda: self.clock[0],
                           verify=lambda origin, host: self.verified.append((origin, host)) or True)
        self.id, self.secret = "a" * 32, "s" * 43

    def tearDown(self):
        self.temp.cleanup()

    def test_claim_is_first_come_and_secret_bound(self):
        self.relay.claim(self.id, self.secret)
        self.relay.claim(self.id, self.secret)  # idempotent for the owner
        with self.assertRaises(RelayError) as caught:
            self.relay.claim(self.id, "t" * 43)
        self.assertEqual(caught.exception.code, "id_taken")
        with self.assertRaises(RelayError) as caught:
            self.relay.beat(self.id, "t" * 43, ORIGIN)
        self.assertEqual(caught.exception.code, "wrong_secret")

    def test_online_only_while_heartbeats_continue(self):
        self.relay.claim(self.id, self.secret)
        self.assertEqual(self.relay.lookup(self.id), {"online": False, "origin": None})
        self.relay.beat(self.id, self.secret, ORIGIN)
        self.assertEqual(self.relay.lookup(self.id), {"online": True, "origin": ORIGIN})
        self.clock[0] += ONLINE_SECONDS + 1
        self.assertEqual(self.relay.lookup(self.id), {"online": False, "origin": None})
        self.relay.beat(self.id, self.secret, ORIGIN)
        self.assertTrue(self.relay.lookup(self.id)["online"])
        self.relay.beat(self.id, self.secret, "")
        self.assertFalse(self.relay.lookup(self.id)["online"])
        # Unchanged origins are not fetched again on every heartbeat.
        self.assertEqual(self.verified, [(ORIGIN, self.id)])

    def test_only_verified_quick_tunnels_are_accepted(self):
        self.relay.claim(self.id, self.secret)
        for origin in ("http://quiet.trycloudflare.com", "https://evil.example", "https://x.trycloudflare.com.evil.example",
                       "https://quiet.trycloudflare.com/s/abc"):
            with self.assertRaises(RelayError) as caught:
                self.relay.beat(self.id, self.secret, origin)
            self.assertEqual(caught.exception.code, "invalid_origin")
        self.relay.verify = lambda origin, host: False
        with self.assertRaises(RelayError) as caught:
            self.relay.beat(self.id, self.secret, OTHER)
        self.assertEqual(caught.exception.code, "origin_not_verified")
        with self.assertRaises(RelayError):
            self.relay.lookup("b" * 32)


class RelayHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.relay = Relay(Path(self.temp.name) / "relay.sqlite3", verify=lambda origin, host: True)
        self.server = relay_server(self.relay, port=0, trust_proxy=False)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.server.server_port

    def tearDown(self):
        self.server.shutdown(); self.server.server_close()
        self.temp.cleanup()

    def get(self, path):
        try:
            with urlopen(self.base + path, timeout=5) as response:
                return response.status, response.read(), response.headers
        except HTTPError as error:
            return error.code, error.read(), error.headers

    def test_host_client_reports_origin_and_goes_offline_on_stop(self):
        with tempfile.TemporaryDirectory() as root:
            public = FakePublic()
            client = RelayLink(root, self.base, public_link=public, poll_seconds=.05)
            # The identity is persistent, so links survive restarts.
            self.assertEqual(RelayLink(root, self.base).host_id, client.host_id)
            self.assertFalse(client.ready)
            link = client.link("0" * 24 + "." + "A" * 43)
            self.assertEqual(link, self.base + "/h/" + client.host_id + "#" + "0" * 24 + "." + "A" * 43)
            client.start()
            try:
                status, body, _ = self.get("/api/hosts/" + client.host_id)
                deadline = time.monotonic() + 3
                while status != 200 and time.monotonic() < deadline:
                    time.sleep(.05); status, body, _ = self.get("/api/hosts/" + client.host_id)
                self.assertEqual(json.loads(body)["online"], False)
                public.info = {"state": "live", "origin": ORIGIN, "error": None}
                deadline = time.monotonic() + 3
                while not json.loads(self.get("/api/hosts/" + client.host_id)[1])["online"] and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertEqual(json.loads(self.get("/api/hosts/" + client.host_id)[1]), {"ok": True, "online": True, "origin": ORIGIN})
                self.assertEqual(client.status()["state"], "online")
                # Acceptance is remembered, so links stay relay links after a restart.
                self.assertTrue(client.ready and RelayLink(root, self.base).ready)
                self.assertFalse(RelayLink(root, "https://other-relay.example").ready)
            finally:
                client.stop()
            self.assertFalse(json.loads(self.get("/api/hosts/" + client.host_id)[1])["online"])

    def test_landing_page_never_needs_the_token(self):
        status, body, headers = self.get("/h/" + "c" * 32)
        self.assertEqual(status, 200)
        self.assertIn(b"/relay.js", body)
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        script = self.get("/relay.js")[1].decode()
        # The token is read from the fragment and sent only to the creator's own address.
        self.assertIn("location.hash", script)
        self.assertNotIn("fetch(\"/api/hosts/\" + host + token", script)
        self.assertEqual(self.get("/api/hosts/" + "c" * 32)[0], 404)
        self.assertEqual(self.get("/h/not-a-host")[0], 404)

    def test_bad_bodies_are_rejected(self):
        def post(path, data, kind="application/json"):
            try:
                with urlopen(Request(self.base + path, data=data, headers={"Content-Type": kind}), timeout=5) as response:
                    return response.status
            except HTTPError as error:
                return error.code
        self.assertEqual(post("/api/hosts", b"{}"), 400)
        self.assertEqual(post("/api/hosts", b"x" * 2000), 400)
        self.assertEqual(post("/api/hosts", json.dumps({"id": "d" * 32, "secret": "s" * 43}).encode(), "text/plain"), 415)
        self.assertEqual(post("/api/hosts", json.dumps({"id": "d" * 32, "secret": "s" * 43}).encode()), 200)
        self.assertEqual(post("/api/hosts/" + "d" * 32 + "/beat", json.dumps({"secret": "s" * 43, "origin": "https://evil.example"}).encode()), 400)


class StableLinkServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manager = CapsuleManager(self.root, lambda s, t, a: {"ok": True, "result": a})
        self.public = FakePublic()
        self.relay = RelayLink(self.root, "https://relay.example")
        self.relay._claimed = True  # as if the relay had accepted this host
        entry = {"id": "srv::tool", "server": "srv", "tool": "tool", "kind": "tool", "title": "Tool", "supported": True,
                 "schema": TOKEN_SCHEMA, "requires_side_effects_approval": False, "safety": "read_only"}
        self.owner = create_server(self.manager, lambda: [entry], port=0, public_link=self.public, relay=self.relay)
        self.guest = create_server(self.manager, lambda: [], port=0, guest_only=True, public_link=self.public, relay=self.relay)
        for server in (self.owner, self.guest):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def tearDown(self):
        for server in (self.owner, self.guest):
            server.shutdown(); server.server_close()
        self.temp.cleanup()

    def call(self, server, path, body=None):
        request = Request("http://127.0.0.1:%d%s" % (server.server_port, path),
                          data=None if body is None else json.dumps(body).encode(),
                          headers={"Authorization": "Bearer " + self.manager.owner_token, "Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def test_public_link_is_stable_before_and_after_the_tunnel_opens(self):
        status, created = self.call(self.owner, "/api/owner/shares", {"tool_id": "srv::tool", "title": "Tool",
                                                                       "public_safe_approved": True, "public": True})
        self.assertEqual(status, 201, created)
        # No tunnel was live: the stable link is issued anyway and the tunnel is asked to open.
        self.assertTrue(created["url"].startswith("https://relay.example/h/" + self.relay.host_id + "#"))
        self.assertEqual(self.public.starts, 1)
        before = self.call(self.owner, "/api/owner/state")[1]
        self.public.info = {"state": "live", "origin": ORIGIN, "error": None}
        after = self.call(self.owner, "/api/owner/state")[1]
        self.assertEqual(before["shares"][0]["url"], after["shares"][0]["url"])
        self.assertEqual((before["shares"][0]["online"], after["shares"][0]["online"]), (False, True))
        self.assertTrue(after["stable_links"])
        # The token in the fragment opens the guest page at the creator's address.
        token = created["url"].split("#", 1)[1]
        with urlopen("http://127.0.0.1:%d/s/%s" % (self.guest.server_port, token), timeout=5) as response:
            self.assertEqual(response.status, 200)
        health = self.call(self.guest, "/healthz")[1]
        self.assertEqual((health["guest_only"], health["relay_host"]), (True, self.relay.host_id))

    def test_unconfirmed_relay_falls_back_to_the_tunnel_address(self):
        self.relay._claimed = False
        body = {"tool_id": "srv::tool", "title": "Tool", "public_safe_approved": True, "public": True}
        status, created = self.call(self.owner, "/api/owner/shares", body)
        self.assertEqual((status, created["code"]), (409, "public_not_ready"))
        self.assertEqual(self.public.starts, 1)
        self.public.info = {"state": "live", "origin": ORIGIN, "error": None}
        status, created = self.call(self.owner, "/api/owner/shares", body)
        self.assertEqual(status, 201, created)
        self.assertTrue(created["url"].startswith(ORIGIN + "/s/"))
        self.assertFalse(self.call(self.owner, "/api/owner/state")[1]["stable_links"])

    def test_local_only_links_do_not_open_the_tunnel(self):
        status, created = self.call(self.owner, "/api/owner/shares", {"tool_id": "srv::tool", "title": "Local",
                                                                       "public_safe_approved": True, "public": False})
        self.assertEqual(status, 201, created)
        self.assertIn("127.0.0.1", created["url"])
        self.assertEqual(self.public.starts, 0)

    def test_active_public_links_are_tracked(self):
        self.assertFalse(self.manager.has_active_public())
        token = self.manager.create(title="t", server="srv", tool="tool", schema=TOKEN_SCHEMA, public_safe_approved=True)
        self.assertTrue(self.manager.has_active_public())
        self.manager.revoke(token)
        self.assertFalse(self.manager.has_active_public())


class AutoReconnectTests(unittest.TestCase):
    def test_failures_retry_with_backoff_until_stopped(self):
        with tempfile.TemporaryDirectory() as root:
            link = PublicLink(root)
            starts = []
            link.guest_port = 1
            original = PublicLink.start
            with unittest.mock.patch.object(tunnel, "RETRY_DELAYS", (.05, .05)), \
                 unittest.mock.patch.object(PublicLink, "_open", lambda self, epoch: None):
                original(link)
                self.assertTrue(link.wanted)
                link.start = lambda: starts.append(1) or original(link)
                link._fail(link._epoch, "boom")
                self.assertEqual(link.status()["retry_in"], .05)
                deadline = time.monotonic() + 2
                while not starts and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(len(starts), 1)
                link.stop()
                link._fail(link._epoch, "after stop")
                time.sleep(.15)
                self.assertEqual(len(starts), 1)
                self.assertFalse(link.wanted)


class LinkDefaultsTests(unittest.TestCase):
    def test_defaults_and_bounds(self):
        from sharelet import CapsuleError
        from sharing import ShareWorkflow
        with tempfile.TemporaryDirectory() as root:
            clock = [5000.0]
            manager = CapsuleManager(Path(root), lambda s, t, a: {"ok": True}, clock=lambda: clock[0])
            entry = {"id": "srv::tool", "server": "srv", "tool": "tool", "kind": "tool", "title": "Tool", "supported": True,
                     "schema": TOKEN_SCHEMA, "requires_side_effects_approval": False, "safety": "read_only"}
            workflow = ShareWorkflow(manager, lambda: [entry])
            token = workflow.create_owned({"tool_id": "srv::tool", "title": "Tool", "public": False,
                                           "public_safe_approved": True}, False)
            info = manager.public_info(token)
            self.assertEqual((info["max_calls"], info["expires_at"] - clock[0]), (20, 7 * 86400))
            workflow.create_owned({"tool_id": "srv::tool", "title": "Long", "public": False, "public_safe_approved": True,
                                   "max_calls": 1000, "expires_seconds": 30 * 86400}, False)
            for changes in ({"max_calls": 1001}, {"expires_seconds": 30 * 86400 + 1}):
                with self.assertRaises(CapsuleError):
                    workflow.create_owned({"tool_id": "srv::tool", "title": "Bad", "public": False,
                                           "public_safe_approved": True, **changes}, False)
            # Without a relay or a live address, a public link waits for the address to open.
            with self.assertRaises(CapsuleError) as caught:
                workflow.create_owned({"tool_id": "srv::tool", "title": "Public", "public": True, "public_safe_approved": True}, False)
            self.assertEqual(caught.exception.code, "public_not_ready")


class ConfigTests(unittest.TestCase):
    def test_relay_base_validation(self):
        self.assertEqual(normalise_base(""), "")
        self.assertEqual(normalise_base("https://relay.example/"), "https://relay.example")
        self.assertEqual(normalise_base("http://127.0.0.1:9"), "http://127.0.0.1:9")
        for bad in ("http://relay.example", "https://relay.example/path", "https://u:p@relay.example", "ftp://relay.example"):
            with self.assertRaises(ValueError):
                normalise_base(bad)


import unittest.mock  # noqa: E402

if __name__ == "__main__":
    unittest.main()
