"""Admission regressions: opt-in relay, profile-owned credentials, pinned binaries.

Windows exercises a context-local SDK fixture; the real Hermes scope is verified
separately on Linux. Image transports and connector bodies here are controlled.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
import base64
import hashlib
import io
import json
from pathlib import Path
import secrets
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

from connections import PortableBridge
from image_provider import ProviderSettings, atomic_private, generate_avatar
from owner_client import OwnerClient
from public_tools import basic_bindings
from relay import DEFAULT_RELAY
from service import Runtime
from sharelet import CapsuleError
from test_images import PNG
from tunnel import CLOUDFLARED_ASSETS, CLOUDFLARED_VERSION, PublicLink, connector_asset


@contextmanager
def profile_sdk():
    scope = ContextVar('review_profile_secrets', default=None)
    module = ModuleType('agent.secret_scope')
    def get_secret(name, default=None):
        values = scope.get()
        if values is None: raise RuntimeError('No multiplex profile scope')
        return values.get(name, default)
    module.get_secret = get_secret
    agent = ModuleType('agent'); agent.secret_scope = module
    with patch.dict(sys.modules, {'agent': agent, 'agent.secret_scope': module}), \
         patch.dict('os.environ', {'OPENAI_API_KEY': 'process-key-belongs-to-a-different-profile'}):
        yield scope


class RelayDefaultsTests(unittest.TestCase):
    def test_default_runtime_does_not_create_or_contact_a_relay(self):
        self.assertEqual(DEFAULT_RELAY, '')
        with tempfile.TemporaryDirectory() as folder, patch('relay.urlopen') as network:
            runtime = Runtime(folder, lambda *a: {'ok': True}, basic_bindings, connection={}, port=0, guest_port=0).start()
            try:
                self.assertIsNone(runtime.relay)
                network.assert_not_called()
                self.assertFalse((Path(folder)/'relay.json').exists())
            finally: runtime.close()

    def test_explicit_creator_relay_still_works(self):
        with tempfile.TemporaryDirectory() as folder, patch('relay.RelayLink.start') as start:
            runtime = Runtime(folder, lambda *a: {'ok': True}, basic_bindings, connection={}, port=0, guest_port=0,
                              relay_url='https://relay.example.com').start()
            try:
                self.assertEqual(runtime.relay.base, 'https://relay.example.com')
                start.assert_called_once()
            finally: runtime.close()


class ProfileCredentialTests(unittest.TestCase):
    def test_profiles_remain_isolated_in_http_and_generation_worker_threads(self):
        key_a, key_b = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with tempfile.TemporaryDirectory() as folder, profile_sdk() as scope:
            runtimes, bridges = [], []
            for name, key in [('a', key_a), ('b', key_b)]:
                scope.set({'OPENAI_API_KEY': key})
                root = Path(folder)/name
                bridge = PortableBridge(root)
                bridges.append(bridge)
                runtimes.append(Runtime(root, bridge.call, bridge.catalog, connection={}, port=0, guest_port=0))
            scope.set(None)
            try:
                for runtime in runtimes:
                    # Starting without a scope also exercises the service-takeover path.
                    runtime.start()
                    owner = OwnerClient(runtime.root, auto_start=False)
                    before = owner.request('/api/owner/state')['provider']
                    self.assertTrue(before['host_key_available']); self.assertFalse(before['configured'])
                    state = owner.request('/api/owner/provider', {'use_host_key': True})
                    self.assertNotIn(key_a, json.dumps(state)); self.assertNotIn(key_b, json.dumps(state))
                    self.assertNotIn(key_a, (runtime.root/'provider.json').read_text())
                    self.assertNotIn(key_b, (runtime.root/'provider.json').read_text())
                response = {'data': [{'b64_json': base64.b64encode(PNG).decode()}]}
                with patch('image_provider._openai_json', return_value=response) as transport, ThreadPoolExecutor(2) as pool:
                    results = list(pool.map(lambda bridge: bridge.call('sharelet_images', 'generate_avatar', {'subject': 'a small fox', 'style': 'clay'}), bridges))
                self.assertTrue(all(result['ok'] for result in results))
                self.assertEqual({call.args[0] for call in transport.call_args_list}, {key_a, key_b})
            finally:
                for runtime in runtimes: runtime.close()
                for bridge in bridges: bridge.close()

    def test_missing_or_unbound_profile_never_uses_process_key(self):
        for mapping in ({}, None):
            with self.subTest(mapping=mapping), tempfile.TemporaryDirectory() as folder, profile_sdk() as scope:
                scope.set(mapping)
                settings = ProviderSettings(folder)
                self.assertFalse(settings.public()['host_key_available'])
                with self.assertRaises(CapsuleError): settings.save({'use_host_key': True})
                atomic_private(settings.path, {'use_host_key': True})
                with patch('image_provider._openai_json') as transport:
                    with self.assertRaises(RuntimeError): generate_avatar('a small fox', 'clay', settings_path=settings.path)
                    transport.assert_not_called()

    def test_owner_entered_key_takes_precedence_and_stays_private(self):
        with tempfile.TemporaryDirectory() as folder, profile_sdk() as scope:
            scope.set({'OPENAI_API_KEY': secrets.token_urlsafe(32)})
            key = secrets.token_urlsafe(32)
            settings = ProviderSettings(folder)
            public = settings.save({'openai_key': key})
            self.assertTrue(public['configured']); self.assertEqual(public['source'], 'owner')
            self.assertNotIn(key, json.dumps(public))


class PinnedConnectorTests(unittest.TestCase):
    def test_every_supported_target_uses_only_its_fixed_release_asset(self):
        for (system, architecture), (asset, digest) in CLOUDFLARED_ASSETS.items():
            with self.subTest(system=system, architecture=architecture), tempfile.TemporaryDirectory() as folder:
                payload = ('controlled '+system+' '+architecture).encode()
                pins = dict(CLOUDFLARED_ASSETS)
                pins[(system, architecture)] = (asset, hashlib.sha256(payload).hexdigest())
                with patch('tunnel.platform.system', return_value=system), patch('tunnel.platform.machine', return_value=architecture), \
                     patch('tunnel.platform.version', return_value='10.0.26100'), patch('tunnel.CLOUDFLARED_ASSETS', pins), \
                     patch('tunnel.urlopen', return_value=io.BytesIO(payload)) as download:
                    connector = PublicLink(folder)
                    connector._install()
                    self.assertTrue(connector._verified_cache())
                    self.assertEqual(connector.bin.read_bytes(), payload)
                    self.assertEqual(download.call_count, 1)
                    url = download.call_args.args[0].full_url
                    self.assertEqual(url, 'https://github.com/cloudflare/cloudflared/releases/download/'+CLOUDFLARED_VERSION+'/'+asset)
                    self.assertNotIn('/latest', url)
                    connector.bin.write_bytes(b'tampered cached executable')
                    self.assertFalse(connector._verified_cache())
                self.assertEqual(len(digest), 64)

    def test_corrupt_cached_binary_is_replaced_before_launch(self):
        with tempfile.TemporaryDirectory() as folder:
            connector = PublicLink(folder); connector.bin.parent.mkdir(); connector.bin.write_bytes(b'corrupt')
            connector.guest_port = 12345
            with patch.object(connector, '_install', side_effect=RuntimeError('checksum failed')) as install, patch('tunnel.subprocess.Popen') as execute:
                connector._open(0)
                install.assert_called_once(); execute.assert_not_called()
                connector.stop()

    def test_old_windows_arm_and_unsupported_targets_fail_without_download(self):
        for system, arch, version in [('Windows', 'ARM64', '10.0.19045'), ('Darwin', 'arm64', '24.0.0'), ('Linux', 'mips', '6.0')]:
            with self.subTest(system=system, arch=arch), tempfile.TemporaryDirectory() as folder, \
                 patch('tunnel.platform.system', return_value=system), patch('tunnel.platform.machine', return_value=arch), \
                 patch('tunnel.platform.version', return_value=version), patch('tunnel.urlopen') as download:
                with self.assertRaises(RuntimeError): PublicLink(folder)._install()
                download.assert_not_called()

    def test_windows_arm_uses_the_official_x64_asset_explicitly(self):
        with patch('tunnel.platform.system', return_value='Windows'), patch('tunnel.platform.machine', return_value='ARM64'), patch('tunnel.platform.version', return_value='10.0.26100'):
            self.assertEqual(connector_asset(), CLOUDFLARED_ASSETS[('windows', 'amd64')])
