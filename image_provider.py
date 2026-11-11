"""Bounded OpenAI image recipe. Credentials and image bytes stay on the host."""
from __future__ import annotations

import base64
import binascii
import json
import os
import secrets
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, HTTPRedirectHandler, build_opener

try:
    from .presets import MODEL, SIZE, STYLES
    from .sharelet import CapsuleError
    from .media import MAX_IMAGE, validate_png
except ImportError:
    from presets import MODEL, SIZE, STYLES
    from sharelet import CapsuleError
    from media import MAX_IMAGE, validate_png


def atomic_private(path, data):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _valid_key(value):
    return (isinstance(value, str) and 16 <= len(value.strip()) <= 500
            and all(33 <= ord(c) <= 126 for c in value.strip()))


def _credential(config):
    """The key the owner entered here, or the host's OPENAI_API_KEY when they chose it.

    Other credential files are never read.
    """
    key = config.get("openai_key")
    if not key and config.get('use_host_key') is True:
        key=os.environ.get('OPENAI_API_KEY')
    return key.strip() if _valid_key(key) else None


class ProviderSettings:
    def __init__(self, root):
        self.path = Path(root) / "provider.json"

    def read(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}

    def public(self):
        try:
            config = self.read()
            configured = bool(_credential(config))
        except (OSError, ValueError):
            config, configured = {}, False
        # No key, path, provider body or credential-derived identifier is returned.
        return {"provider": "openai", "configured": configured, "model": MODEL,
                "size": SIZE, "num_images": 1, "quality": "low", "key_set": configured,
                "host_key_available":_valid_key(os.environ.get('OPENAI_API_KEY')),
                "source": "host" if configured and config.get("use_host_key") is True else "owner"}

    def save(self, body):
        if isinstance(body,dict) and set(body)=={'use_host_key'}:
            if body['use_host_key'] is not True or not _valid_key(os.environ.get('OPENAI_API_KEY')):
                raise CapsuleError('No valid host key is available to enable.')
            atomic_private(self.path,{'provider':'openai','use_host_key':True})
            return self.public()
        if not isinstance(body, dict) or set(body) != {"openai_key"}:
            raise CapsuleError("Only the OpenAI provider key can be changed here.")
        key = body["openai_key"]
        if not _valid_key(key):
            raise CapsuleError("Enter a valid OpenAI API key.")
        atomic_private(self.path, {"provider": "openai", "openai_key": key.strip()})
        return self.public()

    def require_ready(self):
        if not self.public()["configured"]:
            raise CapsuleError("The creator has not connected an image provider yet. No generation was charged.",
                               "provider_not_configured", 503)


def _openai_json(key, payload):
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    request = Request("https://api.openai.com/v1/images/generations",
                      data=json.dumps(payload).encode("utf-8"),
                      headers={"Authorization": "Bearer " + key,
                               "Content-Type": "application/json", "User-Agent": "Sharelet/0.4"})
    # Exactly one submission. A timeout/lost response is never automatically retried.
    with build_opener(NoRedirect()).open(request, timeout=170) as response:
        raw = response.read(12 * 1024 * 1024 + 1)
        if len(raw) > 12 * 1024 * 1024:
            raise RuntimeError("Provider response exceeded the image limit")
        return json.loads(raw)


def generate_avatar(subject, style, model=MODEL, size=SIZE, num_images=1, style_notes="", *, settings_path=None):
    if (not isinstance(subject, str) or not 3 <= len(subject.strip()) <= 400
            or style not in STYLES or model != MODEL or type(size) is not int or size != SIZE
            or type(num_images) is not int or num_images != 1
            or not isinstance(style_notes, str) or len(style_notes) > 300):
        raise RuntimeError("This recipe allows one fixed-size image only")
    configured_path = settings_path or os.environ.get("SHARELET_PROVIDER_CONFIG")
    if not configured_path:
        raise RuntimeError("Image provider is not configured")
    path = Path(configured_path)
    try:
        config = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        key = _credential(config)
    except (OSError, ValueError):
        key = None
    if not key:
        raise RuntimeError("Image provider is not configured")
    prompt = STYLES[style]["prefix"] + subject.strip()
    if style_notes.strip():
        prompt += ". Additional art direction: " + style_notes.strip()
    payload = {"model": MODEL, "prompt": prompt, "size": f"{SIZE}x{SIZE}", "n": 1,
               "quality": "low", "output_format": "png", "background": "opaque",
               "moderation": "auto"}
    try:
        result = _openai_json(key, payload)
        images = result.get("data") or []
        if len(images) != 1 or not isinstance(images[0], dict):
            raise ValueError("Expected one image")
        encoded = images[0].get("b64_json")
        if not isinstance(encoded, str) or len(encoded) > (MAX_IMAGE * 4 // 3 + 8):
            raise ValueError("Invalid image payload")
        image = base64.b64decode(encoded, validate=True)
        validate_png(image)
        # MCP has a small JSON envelope. Return an opaque receipt, never megabytes of base64.
        outbox = path.parent / "provider-outbox"
        outbox.mkdir(mode=0o700, parents=True, exist_ok=True)
        asset_id = secrets.token_hex(16)
        fd = os.open(outbox / (asset_id + ".png"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(image)
        return {"kind": "image", "asset_id": asset_id, "width": SIZE, "height": SIZE, "model": MODEL}
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError, binascii.Error, CapsuleError):
        # Never forward provider error bodies or authentication/transport details to guests.
        raise RuntimeError("Image provider request failed; no automatic resubmission was made") from None
