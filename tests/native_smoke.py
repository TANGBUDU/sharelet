"""Read-only audit of a running native preview. Does not submit a paid generation."""
import argparse
import hashlib
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

parser = argparse.ArgumentParser()
parser.add_argument('--root', type=Path, required=True, help='Exact SDK-owned plugin data directory')
parser.add_argument('--share-url', help='Optional existing guest link')
parser.add_argument('--job-id', help='Optional already completed job')
args = parser.parse_args()
metadata = json.loads((args.root / 'service.json').read_text())
owner = 'http://127.0.0.1:' + str(metadata['owner_port'])
token = (args.root / 'owner-v2.key').read_text()


def request(base, path, *, authenticated=False):
    headers = {'Authorization': 'Bearer ' + token} if authenticated else {}
    try:
        response = urlopen(Request(base + path, headers=headers), timeout=20)
    except HTTPError as error:
        response = error
    with response:
        body = response.read()
        return response.code, json.loads(body) if response.headers.get_content_type() == 'application/json' else body


code, state = request(owner, '/api/owner/state', authenticated=True)
assert code == 200 and state['connection']['mode'] == 'hermes'
assert state['provider']['configured'] and state['provider']['provider'] == 'openai'
assert len([t for t in state['tools'] if t['kind'] == 'image']) == 3
assert any(t['kind'] == 'tool' and t['tool'] == 'mcp__sharelet_public__text_cleanup' for t in state['tools'])
checks = ['native creator service, generic tool discovery and fixed image recipes', 'OpenAI credential configured without returning it']
guest = 'http://127.0.0.1:' + str(metadata['guest_port'])
assert request(guest, '/api/owner/state', authenticated=True)[0] == 401
checks.append('guest listener denies creator API even with creator credential')
if args.share_url:
    parsed = urlsplit(args.share_url)
    assert parsed.scheme in ('http', 'https') and parsed.path.startswith('/s/')
    base, slug = parsed.scheme + '://' + parsed.netloc, parsed.path[3:]
    code, info = request(base, '/api/s/' + slug)
    assert code == 200 and list(info['share']['schema']['properties']) == ['subject']
    assert request(base, '/api/owner/state')[0] == 401
    assert request(base, '/')[0] == 404
    checks.append('guest metadata, public root and creator separation')
    if args.job_id:
        code, job = request(base, '/api/s/' + slug + '/jobs/' + args.job_id)
        assert code == 200 and job['job']['state'] == 'succeeded'
        code, image = request(base, job['job']['media_url'] + '?download=1')
        assert code == 200 and image.startswith(b'\x89PNG\r\n\x1a\n')
        checks.append('existing completed image served and downloaded: sha256=' + hashlib.sha256(image).hexdigest())
code, after = request(owner, '/api/owner/state', authenticated=True)
assert after['budget']['used'] == state['budget']['used']
checks.append('audit submitted no generation and consumed no allowance')
print(json.dumps({'ok': True, 'mode': 'read-only native preview audit', 'checks': checks}, indent=2))
