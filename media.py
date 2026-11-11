"""Deliver fixed-size PNGs through the share capability; no arbitrary URLs or paths."""
import json
import os
import re
import secrets
import struct
import zlib
from pathlib import Path

try:
    from .sharelet import CapsuleError
    from .presets import SIZE
except ImportError:
    from sharelet import CapsuleError
    from presets import SIZE

MAX_IMAGE = 8 * 1024 * 1024


def unwrap_result(value):
    for _ in range(5):
        if isinstance(value, str):
            try:
                value = json.loads(value)
                continue
            except ValueError:
                break
        if isinstance(value, dict) and isinstance(value.get("content"), list):
            texts = [item.get("text", "") for item in value["content"]
                     if isinstance(item, dict) and item.get("type") == "text"]
            try:
                value = json.loads("\n".join(texts))
                continue
            except ValueError:
                break
        if isinstance(value, dict) and set(value) == {"result"}:
            value = value["result"]
            continue
        break
    return value


def validate_png(data):
    if (not isinstance(data, bytes) or not 57 <= len(data) <= MAX_IMAGE
            or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR"
            or struct.unpack(">II", data[16:24]) != (SIZE, SIZE)):
        raise CapsuleError("The provider did not return the expected PNG image.", "invalid_media", 502)
    cursor, saw_data, ended = 8, False, False
    while cursor + 12 <= len(data):
        length = struct.unpack(">I", data[cursor:cursor+4])[0]
        end = cursor + length + 12
        if end > len(data):
            break
        kind = data[cursor+4:cursor+8]
        checksum = struct.unpack(">I", data[end-4:end])[0]
        if zlib.crc32(data[cursor+4:end-4]) & 0xffffffff != checksum:
            break
        if kind == b"IDAT":
            saw_data = True
        if kind == b"IEND":
            ended = length == 0 and end == len(data)
            break
        cursor = end
    if not saw_data or not ended:
        raise CapsuleError("The generated image was incomplete.", "invalid_media", 502)


class MediaStore:
    def __init__(self, root):
        self.root = Path(root) / "media"
        self.outbox = Path(root) / "provider-outbox"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def save(self, job_id, response):
        value = unwrap_result(response)
        asset = value.get("asset_id") if isinstance(value, dict) else None
        if (not isinstance(value, dict) or value.get("kind") != "image" or not isinstance(asset, str)
                or not re.fullmatch(r"[a-f0-9]{32}", asset)):
            raise CapsuleError("The tool did not return an image receipt.", "invalid_media", 502)
        source = self.outbox / (asset + ".png")
        # The receipt cannot name a file outside this profile's fixed private outbox.
        if source.is_symlink() or source.resolve().parent != self.outbox.resolve():
            raise CapsuleError("Image receipt not found.", "invalid_media", 502)
        fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            image = stream.read(MAX_IMAGE + 1)
        validate_png(image)
        path = self.path(job_id)
        temporary = path.with_suffix("." + secrets.token_hex(6) + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(image)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        source.unlink(missing_ok=True)
        return {"width": SIZE, "height": SIZE, "content_type": "image/png"}

    def path(self, job_id):
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise CapsuleError("Image not found.", "unavailable", 404)
        return self.root / (job_id + ".png")
