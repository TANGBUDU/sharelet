"""Optional real-SDK profile isolation check; image HTTP is mocked, never billed.

Run with --hermes-source pointing to a Hermes checkout. Ordinary unit tests do
not require that checkout. All profiles and credentials here are temporary fixtures.
"""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--hermes-source', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(root), str(root/'tests'), str(args.hermes_source.resolve())]
    from agent.secret_scope import set_multiplex_active, set_secret_scope, reset_secret_scope
    from connections import PortableBridge
    from image_provider import ProviderSettings
    from owner_client import OwnerClient
    from service import Runtime
    from test_images import PNG

    keys = ('fixture-profile-a-secret-123456', 'fixture-profile-b-secret-654321')
    set_multiplex_active(True)
    runtimes, bridges = [], []
    try:
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'OPENAI_API_KEY': 'wrong-process-profile-secret-123456'}):
            for name, key in zip(('a', 'b'), keys):
                home = Path(folder)/name
                token = set_secret_scope({'OPENAI_API_KEY': key}, profile_home=str(home))
                try:
                    bridge = PortableBridge(home/'sharelet')
                    bridges.append(bridge)
                    runtimes.append(Runtime(home/'sharelet', bridge.call, bridge.catalog, connection={}, port=0, guest_port=0))
                finally:
                    reset_secret_scope(token)
            try:
                for runtime in runtimes:
                    # No profile scope is bound in this caller or the HTTP threads.
                    runtime.start()
                    owner = OwnerClient(runtime.root, auto_start=False)
                    state = owner.request('/api/owner/state')
                    assert state['relay'] is None
                    assert state['provider']['host_key_available'] and not state['provider']['configured']
                    owner.request('/api/owner/provider', {'use_host_key': True})
                    assert not any(key in (runtime.root/'provider.json').read_text() for key in keys)
                response = {'data': [{'b64_json': base64.b64encode(PNG).decode()}]}
                with patch('image_provider._openai_json', return_value=response) as transport, ThreadPoolExecutor(2) as pool:
                    results = list(pool.map(lambda bridge: bridge.call('sharelet_images', 'generate_avatar', {'subject': 'a small fox', 'style': 'clay'}), bridges))
                assert all(value['ok'] for value in results)
                assert {call.args[0] for call in transport.call_args_list} == set(keys)
                for mapping in ({}, None):
                    token = set_secret_scope(mapping)
                    try:
                        settings = ProviderSettings(Path(folder)/'missing')
                        assert not settings.public()['host_key_available']
                    finally:
                        reset_secret_scope(token)
                report = {'ok': True, 'real_hermes_secret_scope': True, 'profiles': 2,
                          'unscoped_http_workers_use_owner_scope': True, 'generation_workers_use_owner_scope': True,
                          'missing_or_unbound_scope_ignores_process_key': True, 'owner_consent_required': True,
                          'keys_not_persisted': True, 'default_relay_disabled': True, 'paid_api_calls': 0,
                          'hermes_commit': subprocess.check_output(['git', '-C', str(args.hermes_source), 'rev-parse', 'HEAD']).decode().strip()}
                if args.output: args.output.write_text(json.dumps(report, indent=2))
                print(json.dumps(report))
            finally:
                for runtime in runtimes: runtime.close()
                for bridge in bridges: bridge.close()
    finally:
        set_multiplex_active(False)


if __name__ == '__main__': main()
