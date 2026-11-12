"""HTTP checks for image capability boundaries. Providers are controlled fixtures."""
import hashlib
import io
import json
from pathlib import Path
import secrets
import tempfile
import time
import threading
import unittest
import zipfile
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from image_provider import ProviderSettings
from presets import image_bindings
from service import Runtime
from test_images import PNG
from tunnel import PublicLink


class ImageHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.calls = []
        root = Path(self.temp.name)
        def dispatch(server, tool, args):
            self.calls.append(args)
            asset = secrets.token_hex(16)
            outbox = root / 'provider-outbox'
            outbox.mkdir(exist_ok=True)
            (outbox / (asset + '.png')).write_bytes(PNG)
            return {'ok': True, 'result': {'kind': 'image', 'asset_id': asset}}
        self.runtime = Runtime(root, dispatch, image_bindings, connection={'mode': 'test'},
                               port=0, guest_port=0).start()
        self.owner = 'http://127.0.0.1:' + str(self.runtime.info['owner_port'])
        self.guest = 'http://127.0.0.1:' + str(self.runtime.info['guest_port'])
        ProviderSettings(root).save({'openai_key': secrets.token_urlsafe(32)})
        self.token = self.runtime.manager.owner_token

    def tearDown(self):
        self.runtime.close()
        self.runtime.jobs.executor.shutdown(wait=True, cancel_futures=True)
        self.temp.cleanup()

    def request(self, base, path, body=None, *, owner=False, headers=None):
        headers = {'Content-Type': 'application/json', **(headers or {})}
        if owner:
            headers['Authorization'] = 'Bearer ' + self.token
        request = Request(base + path, headers=headers,
                          data=None if body is None else json.dumps(body).encode())
        try:
            response = urlopen(request, timeout=3)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read()
            data = json.loads(raw) if response.headers.get_content_type() == 'application/json' else raw
            return response.code, data, response.headers

    def create(self, public=True, **changes):
        if public:
            # A controlled controller state, not a real tunnel claim.
            self.runtime.public._info = {'state': 'live', 'origin': 'https://unit.trycloudflare.com', 'error': None}
        body = dict(tool_id=image_bindings()[0]['id'], title='My studio', description='For friends',
                    max_calls=5, expires_seconds=3600, public_safe_approved=True,
                    public=public, style_notes='Sage green scarf')
        body.update(changes)
        code, data, _ = self.request(self.owner, '/api/owner/shares', body, owner=True)
        self.assertEqual(code, 201, data)
        return data['url'].split('/s/')[1]

    def submit(self, slug):
        body = {'input': {'subject': 'a tiny fox'}, 'request_id': secrets.token_hex(16)}
        code, data, _ = self.request(self.guest, '/api/s/' + slug + '/jobs', body)
        self.assertEqual(code, 202, data)
        return body, data['job']

    def finish(self, slug, job):
        for _ in range(100):
            code, data, _ = self.request(self.guest, '/api/s/' + slug + '/jobs/' + job['id'])
            if data['job']['state'] not in ('queued', 'running'):
                self.assertEqual(data['job']['state'], 'succeeded', data)
                return data['job']
            time.sleep(.01)
        self.fail('Controlled job did not finish')

    def test_private_preview_is_not_available_on_guest_listener(self):
        slug = self.create(public=False)
        for path, body in [('/s/' + slug, None), ('/api/s/' + slug, None),
                           ('/api/s/' + slug + '/jobs', {'input': {'subject': 'a fox'},
                                                          'request_id': secrets.token_hex(16)})]:
            self.assertEqual(self.request(self.guest, path, body)[0], 404)
        self.assertEqual(self.request(self.owner, '/s/' + slug)[0], 200)
        self.assertEqual(self.runtime.jobs.budget()['used'], 0)

    def test_image_http_flow_is_idempotent_and_last_result_downloads(self):
        slug = self.create(max_calls=1)
        body, job = self.submit(slug)
        job = self.finish(slug, job)
        repeated = self.request(self.guest, '/api/s/' + slug + '/jobs', body)
        self.assertEqual(repeated[0], 202)
        self.assertEqual(repeated[1]['job']['id'], job['id'])
        recovered = self.request(self.guest, '/api/s/' + slug + '/requests/' + body['request_id'])
        self.assertEqual(recovered[1]['job']['id'], job['id'])
        code, image, headers = self.request(self.guest, job['media_url'] + '?download=1')
        self.assertEqual((code, image), (200, PNG))
        self.assertIn('attachment', headers['Content-Disposition'])
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(self.calls[0]['style_notes'], 'Sage green scarf')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.runtime.jobs.budget()['used'], 1)
        self.assertEqual(self.request(self.guest, '/api/s/' + slug,
                                     {'subject': 'bypass jobs'})[1]['code'], 'image_job_required')

    def test_guest_cannot_access_owner_or_choose_locked_recipe_arguments(self):
        slug = self.create()
        for path, body in [('/api/owner/state', None), ('/api/owner/provider', {'openai_key': 'x'*24}),
                           ('/api/owner/public/start', {})]:
            self.assertEqual(self.request(self.guest, path, body, owner=True)[0], 401)
        for extra in ({'style_notes': 'Override owner'}, {'quality': 'high'}, {'model': 'other-model'}):
            body = {'input': {'subject': 'a tiny fox', **extra}, 'request_id': secrets.token_hex(16)}
            self.assertEqual(self.request(self.guest, '/api/s/' + slug + '/jobs', body)[0], 400)
        code, info, _ = self.request(self.guest, '/api/s/' + slug)
        self.assertEqual(list(info['share']['schema']['properties']), ['subject'])
        self.assertNotIn('openai_key', json.dumps(info))
        self.assertEqual(self.runtime.jobs.budget()['used'], 0)

    def test_revocation_blocks_saved_image_and_job_urls(self):
        slug = self.create()
        _, job = self.submit(slug)
        job = self.finish(slug, job)
        self.assertEqual(self.request(self.owner, '/api/owner/shares/' + slug.split('.')[0] + '/revoke',
                                     {}, owner=True)[0], 200)
        self.assertEqual(self.request(self.guest, job['media_url'])[0], 410)
        self.assertEqual(self.request(self.guest, '/api/s/' + slug + '/jobs/' + job['id'])[0], 410)

    def test_public_image_creation_waits_for_a_verified_public_address(self):
        # Asking for a public link opens the address; do not download a real connector here.
        with patch.object(self.runtime.public, 'start', return_value={'state': 'connecting'}) as start:
            code, _, _ = self.request(self.owner, '/api/owner/shares',
                                      {'tool_id': image_bindings()[0]['id'], 'title': 'My studio',
                                       'public_safe_approved': True, 'public':True}, owner=True)
        self.assertEqual(code, 409)
        self.assertEqual(start.call_count, 1)
        self.assertEqual(self.runtime.manager.list_shares(), [])

    def test_network_host_must_match_owned_tunnel_address(self):
        slug = self.create()
        self.assertEqual(self.request(self.guest, '/s/' + slug,
                                     headers={'Host': 'unit.trycloudflare.com'})[0], 200)
        self.assertEqual(self.request(self.guest, '/s/' + slug,
                                     headers={'Host': 'different.trycloudflare.com'})[0], 421)

    def test_plugin_download_includes_source_and_excludes_private_runtime_files(self):
        code, body, headers = self.request(self.guest, '/download/sharelet.zip')
        self.assertEqual(code, 200)
        self.assertEqual(headers.get_content_type(), 'application/zip')
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            names = archive.namelist()
            self.assertIn('sharelet/plugin.yaml', names)
            self.assertIn('sharelet/distribution.py', names)
            self.assertTrue(all(name.startswith('sharelet/') for name in names))
            self.assertFalse(any(part.startswith('.') or part in ('provider.json','owner.key','owner-v2.key','agent.key','link.key','__pycache__')
                                 for name in names for part in name.split('/')))
            self.assertNotIn(self.token.encode(), b''.join(archive.read(name) for name in names))


class TunnelTests(unittest.TestCase):
    def test_silent_connector_cannot_remain_connecting_forever(self):
        closed = threading.Event()
        class SilentOutput:
            def __iter__(self):
                closed.wait(4)
                return iter(())
        class Process:
            stdout = SilentOutput()
            def poll(self): return 0 if closed.is_set() else None
            def terminate(self): closed.set()
            def wait(self, timeout=None): return 0
        with tempfile.TemporaryDirectory() as folder:
            connector = PublicLink(folder, binary=Path(folder)/'bin'/'test-connector')
            connector.bin.parent.mkdir()
            connector.bin.write_bytes(b'existing controlled connector')
            connector.guest_port = 12345
            with patch('tunnel.CONNECT_TIMEOUT', .3), patch('tunnel.subprocess.Popen', return_value=Process()):
                connector.start()
                for _ in range(100):
                    if connector.status()['state'] == 'failed': break
                    time.sleep(.01)
                self.assertEqual(connector.status()['state'], 'failed')
                self.assertIn('timed out', connector.status()['error'])
                self.assertTrue(closed.wait(1))
                connector.stop()

    def test_connector_checksum_mismatch_cannot_install_executable(self):
        with tempfile.TemporaryDirectory() as folder:
            connector = PublicLink(folder)
            responses = [io.BytesIO(b'wrong binary')]
            with patch('tunnel.platform.system', return_value='Windows'), \
                 patch('tunnel.platform.machine', return_value='AMD64'), \
                 patch('tunnel.urlopen', side_effect=responses):
                with self.assertRaisesRegex(RuntimeError, 'checksum'):
                    connector._install()
            self.assertFalse(connector.bin.exists())
            self.assertEqual(list(connector.bin.parent.glob('*.download-*')), [])

    def test_connector_does_not_inherit_image_provider_secrets(self):
        # The subprocess environment allowlist is also checked here without launching a real tunnel.
        with tempfile.TemporaryDirectory() as folder:
            connector = PublicLink(folder, binary=Path(folder)/'bin'/'test-connector')
            connector.bin.parent.mkdir()
            connector.bin.write_bytes(b'exists')
            connector.guest_port = 12345
            with patch.dict('os.environ', {'OPENAI_API_KEY': 'never-in-child-env'}), \
                 patch('tunnel.subprocess.Popen', side_effect=RuntimeError('stop before launch')) as launch:
                connector._open(0)
            self.assertNotIn('OPENAI_API_KEY', launch.call_args.kwargs['env'])
            self.assertEqual(launch.call_args.args[0][-1], 'http://127.0.0.1:12345')


if __name__ == '__main__':
    unittest.main(verbosity=2)
