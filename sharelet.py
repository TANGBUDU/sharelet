"""Sharelet's capability links, constrained inputs and durable call accounting.

Only an authenticated owner can create a capability. The guest never chooses
the MCP identity, fixed arguments, schema or output adapter.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

MAX_REQ = 16384
MAX_LINK_RUNS = 1000
MAX_LINK_SECONDS = 30 * 86400
DEFAULT_LINK_RUNS = 20
DEFAULT_LINK_SECONDS = 7 * 86400
MAX_RESULT = 32000
MAX_FIELDS = 8
_FIELD_KEYS = {"type", "title", "description", "default", "examples", "enum", "const",
               "minLength", "maxLength", "minimum", "maximum", "nullable", "format"}
_ROOT_KEYS = {"type", "properties", "required", "additionalProperties", "title",
              "description", "$schema"}


class CapsuleError(Exception):
    def __init__(self, message, code="invalid_input", status=400):
        super().__init__(message)
        self.code, self.status = code, status


class DispatchNotSent(Exception):
    """A dispatch precondition failed before the underlying tool was contacted."""
    def __init__(self, message, code="not_sent"):
        super().__init__(message)
        self.code = code


_dispatch_guard = ContextVar("sharelet_dispatch_guard", default=None)


def current_dispatch_guard():
    # Bridges capture this callback before crossing a thread or async queue boundary.
    return _dispatch_guard.get()


@contextmanager
def dispatch_scope(check):
    token = _dispatch_guard.set(check)
    try:
        check()
        yield
    finally:
        _dispatch_guard.reset(token)


def private_file(path: Path, content: bytes):
    """Create a secret once; avoid both truncation and concurrent startup races."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_bytes()
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    return content


def normalise_schema(spec):
    """Supported JSON Schema subset. Unsupported validation NEVER disappears."""
    if not isinstance(spec, dict) or spec.get("type") != "object":
        raise CapsuleError("Only object forms are supported.")
    unsupported = set(spec) - _ROOT_KEYS
    if unsupported:
        raise CapsuleError("Unsupported rules: " + ", ".join(sorted(unsupported)))
    properties = spec.get("properties", {})
    if not isinstance(properties, dict) or len(properties) > MAX_FIELDS:
        raise CapsuleError("A recipe can have at most 8 fields.")
    if spec.get("additionalProperties", False) is not False:
        raise CapsuleError("Open-ended parameters are not supported.")
    required = spec.get("required", [])
    if (not isinstance(required, list) or any(not isinstance(v, str) for v in required)
            or len(set(required)) != len(required) or set(required) - set(properties)):
        raise CapsuleError("Invalid required fields.")
    result = {"type": "object", "properties": {}, "required": list(required),
              "additionalProperties": False}
    for name, original in properties.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name):
            raise CapsuleError("Invalid field name.")
        if not isinstance(original, dict):
            raise CapsuleError("Invalid field: " + name)
        field = copy.deepcopy(original)
        if 'anyOf' in field:
            branches=field.pop('anyOf')
            if (not isinstance(branches,list) or len(branches)!=2
                    or not all(isinstance(branch,dict) for branch in branches)
                    or sum(branch=={'type':'null'} for branch in branches)!=1):
                raise CapsuleError(name+': only a scalar or null is supported.')
            scalar=next(branch for branch in branches if branch!={'type':'null'})
            if any(key in field and field[key]!=value for key,value in scalar.items()):
                raise CapsuleError(name+': conflicting nullable rules.')
            field.update(scalar);field['nullable']=True
        if isinstance(field.get('type'),list):
            types=field['type']
            if len(types)!=2 or types.count('null')!=1 or not all(isinstance(value,str) for value in types):
                raise CapsuleError(name+': only a scalar or null is supported.')
            field['type']=next(value for value in types if value!='null');field['nullable']=True
        if 'nullable' in field and type(field['nullable']) is not bool:
            raise CapsuleError(name+': invalid nullable rule.')
        unsupported = set(field) - _FIELD_KEYS
        if unsupported:
            raise CapsuleError(name + " has unsupported rules: " + ", ".join(sorted(unsupported)))
        kind = field.get("type")
        if kind not in ("string", "integer", "number", "boolean"):
            raise CapsuleError(name + ": nested objects, arrays and unions are not supported.")
        if 'format' in field and (kind!='string' or field['format']!='sharelet-pdf-uploads' or field.get('nullable')):
            raise CapsuleError(name+': unsupported file format.')
        if kind == "string":
            field.setdefault("minLength", 0)
            field.setdefault("maxLength", 500)
            if (type(field["minLength"]) is not int or type(field["maxLength"]) is not int
                    or not 0 <= field["minLength"] <= field["maxLength"] <= 500):
                raise CapsuleError(name + ": string bounds must be within 0-500.")
        if kind in ("integer", "number"):
            field.setdefault("minimum", -1000000)
            field.setdefault("maximum", 1000000)
            if (any(type(field[k]) not in (int, float) or not math.isfinite(field[k])
                    for k in ("minimum", "maximum"))
                    or not -1000000 <= field["minimum"] <= field["maximum"] <= 1000000):
                raise CapsuleError(name + ": invalid numeric bounds.")
        if "enum" in field:
            values = field["enum"]
            if not isinstance(values, list) or not 1 <= len(values) <= 30:
                raise CapsuleError(name + ": invalid allowed values.")
            for value in values:
                _validate_field(name, {k: v for k, v in field.items() if k not in ("enum", "const")}, value)
        if "const" in field:
            _validate_field(name, {k: v for k, v in field.items() if k != "const"}, field["const"])
        result["properties"][name] = field
    return result


def _same_json(left, right):
    # JSON booleans must not compare equal to 0 or 1.
    return type(left) is type(right) and left == right


def _validate_field(name, field, value):
    if value is None and field.get('nullable'):
        if 'enum' in field and None not in field['enum']:
            raise CapsuleError(name+': select an allowed value.')
        if 'const' in field and field['const'] is not None:
            raise CapsuleError(name+': this parameter is fixed by the creator.')
        return
    kind = field["type"]
    good = (isinstance(value, str) if kind == "string" else
            type(value) is int if kind == "integer" else
            type(value) in (int, float) and math.isfinite(value) if kind == "number" else
            type(value) is bool)
    if not good:
        raise CapsuleError(name + ": incorrect field type.")
    if kind == "string" and not field.get("minLength", 0) <= len(value) <= field.get("maxLength", 500):
        raise CapsuleError(name + ": text length is outside the allowed range.")
    if kind in ("integer", "number") and not field.get("minimum", -1000000) <= value <= field.get("maximum", 1000000):
        raise CapsuleError(name + ": number is outside the allowed range.")
    if "enum" in field and not any(_same_json(value, v) for v in field["enum"]):
        raise CapsuleError(name + ": select an allowed value.")
    if "const" in field and not _same_json(value, field["const"]):
        raise CapsuleError(name + ": this parameter is fixed by the creator.")


def validate_values(spec, args, *, partial=False):
    if not isinstance(args, dict) or set(args) - set(spec["properties"]):
        raise CapsuleError("Unexpected form fields.")
    if not partial and set(spec.get("required", [])) - set(args):
        raise CapsuleError("Complete all required fields.")
    for name, value in args.items():
        _validate_field(name, spec["properties"][name], value)
    return True


_validate_form = normalise_schema
_validate_values = validate_values


def local_identity_proof(token, profile, challenge, role, port):
    message = ('sharelet-local-v2\0'+role+'\0'+str(profile)+'\0'+str(port)+'\0'+challenge).encode()
    return hmac.digest(token.encode(), message, 'sha256').hex()


class CapsuleManager:
    """SQLite transactions coordinate quotas across threads and worker processes."""
    def __init__(self, root: str | Path, call_tool, *, clock=time.time):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.root / "sharelet.sqlite3"
        self._call_tool, self._clock = call_tool, clock
        self._key = private_file(self.root / "link.key", secrets.token_bytes(32))
        # Do not reuse the pre-0.4.1 credential: old MCP processes read owner.key
        # on every request and would otherwise retain publishing privileges.
        self.owner_token = private_file(self.root / "owner-v2.key", secrets.token_urlsafe(32).encode()).decode()
        self.agent_token = private_file(self.root / "agent.key", secrets.token_urlsafe(32).encode()).decode()
        with self._db() as db:
            db.executescript("""
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS shares (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL,
                server TEXT NOT NULL, tool TEXT NOT NULL, tool_id TEXT NOT NULL,
                schema_json TEXT NOT NULL, fixed_json TEXT NOT NULL,
                max_calls INTEGER NOT NULL, calls INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL, expires_at REAL NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1
              );
              CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, share_id TEXT NOT NULL, state TEXT NOT NULL,
                started_at REAL NOT NULL, finished_at REAL, duration_ms INTEGER,
                FOREIGN KEY(share_id) REFERENCES shares(id)
              );
            """)
            db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in db.execute("PRAGMA table_info(shares)")}
            if "kind" not in columns:
                db.execute("ALTER TABLE shares ADD COLUMN kind TEXT NOT NULL DEFAULT 'tool'")
            if "preset" not in columns:
                db.execute("ALTER TABLE shares ADD COLUMN preset TEXT NOT NULL DEFAULT ''")
            if "public_enabled" not in columns:
                db.execute("ALTER TABLE shares ADD COLUMN public_enabled INTEGER NOT NULL DEFAULT 1")
            if "side_effects_approved" not in columns:
                db.execute("ALTER TABLE shares ADD COLUMN side_effects_approved INTEGER NOT NULL DEFAULT 0")
            if "tool_review_hash" not in columns:
                db.execute("ALTER TABLE shares ADD COLUMN tool_review_hash TEXT")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _token(self, share_id):
        signature = base64.urlsafe_b64encode(hmac.digest(self._key, share_id.encode(), "sha256")).decode().rstrip("=")
        return share_id + "." + signature

    def _id(self, token):
        if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{24}\.[A-Za-z0-9_-]{43}", token):
            raise CapsuleError("This share link is unavailable.", "unavailable", 404)
        share_id = token.split(".")[0]
        if not re.fullmatch(r"[a-f0-9]{24}", share_id) or not hmac.compare_digest(token, self._token(share_id)):
            raise CapsuleError("This share link is unavailable.", "unavailable", 404)
        return share_id

    def _status(self, row):
        if not row["enabled"]:
            return "revoked"
        if self._clock() >= row["expires_at"]:
            return "expired"
        if row["calls"] >= row["max_calls"]:
            return "exhausted"
        return "active"

    def _decode(self, row, *, owner=False):
        data = {key: row[key] for key in ("id", "title", "description", "max_calls", "calls", "created_at", "expires_at", "kind", "preset")}
        data["public_enabled"] = bool(row["public_enabled"])
        schema = json.loads(row["schema_json"])
        fixed = json.loads(row["fixed_json"])
        data.update(status=self._status(row), remaining=max(0, row["max_calls"] - row["calls"]),
                    schema={**schema, "properties": {k: v for k, v in schema["properties"].items() if k not in fixed},
                            "required": [k for k in schema["required"] if k not in fixed]})
        if owner:
            data.update(server=row["server"], tool=row["tool"], tool_id=row["tool_id"],
                        fixed_args=fixed, token=self._token(row["id"]))
        if row["kind"] == "image":
            data.update(image_size=fixed.get("size", 512), num_images=1, model=fixed.get("model"))
        return data

    def create(self, *, title, server, tool, schema, max_calls=10, expires_seconds=3600,
               public_safe_approved=False, description="", fixed_args=None, tool_id=None, kind="tool", preset="", public_enabled=True,
               side_effects_approved=False, tool_review_hash=None):
        if public_safe_approved is not True:
            raise CapsuleError("Approve public use of this exact recipe before sharing it.")
        spec = self.prepare(title=title, server=server, tool=tool, schema=schema, max_calls=max_calls,
                            expires_seconds=expires_seconds, description=description, fixed_args=fixed_args,
                            tool_id=tool_id, kind=kind, preset=preset, public_enabled=public_enabled)
        spec.update(side_effects_approved=side_effects_approved, tool_review_hash=tool_review_hash)
        with self._db() as db:
            return self._insert_share(db, spec)

    @staticmethod
    def prepare(*, title, server, tool, schema, max_calls=10, expires_seconds=3600,
                description="", fixed_args=None, tool_id=None, kind="tool", preset="", public_enabled=True):
        if any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", v) for v in (server, tool)):
            raise CapsuleError("Invalid tool identity.")
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 80:
            raise CapsuleError("Enter a share title of 1-80 characters.")
        if not isinstance(description, str) or len(description) > 240:
            raise CapsuleError("The description can have at most 240 characters.")
        if type(max_calls) is not int or not 1 <= max_calls <= MAX_LINK_RUNS:
            raise CapsuleError("Choose a link allowance between 1 and 1000.")
        if type(expires_seconds) is not int or not 60 <= expires_seconds <= MAX_LINK_SECONDS:
            raise CapsuleError("Choose an expiry between 1 minute and 30 days.")
        schema = normalise_schema(schema)
        if kind not in ("tool", "image") or not isinstance(preset, str) or len(preset) > 40:
            raise CapsuleError("Invalid recipe type.")
        if type(public_enabled) is not bool:
            raise CapsuleError("Invalid share audience.")
        fixed = {} if fixed_args is None else copy.deepcopy(fixed_args)
        validate_values(schema, fixed, partial=True)
        # A const is inherently fixed; never let a guest supply a competing value.
        for name, field in schema["properties"].items():
            if "const" in field:
                fixed[name] = field["const"]
        return dict(title=title.strip(), description=description.strip(), server=server, tool=tool,
                    tool_id=tool_id or server+"::"+tool, schema=schema, fixed_args=fixed, max_calls=max_calls,
                    expires_seconds=expires_seconds, kind=kind, preset=preset, public_enabled=public_enabled)

    def _insert_share(self, db, spec):
        share_id, now = secrets.token_hex(12), self._clock()
        db.execute("INSERT INTO shares (id,title,description,server,tool,tool_id,schema_json,fixed_json,max_calls,created_at,expires_at,kind,preset,public_enabled,side_effects_approved,tool_review_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (share_id, spec["title"], spec["description"], spec["server"], spec["tool"], spec["tool_id"],
                    json.dumps(spec["schema"], ensure_ascii=False), json.dumps(spec["fixed_args"], ensure_ascii=False),
                    spec["max_calls"], now, now+spec["expires_seconds"], spec["kind"], spec["preset"],
                    int(spec["public_enabled"]), int(spec.get("side_effects_approved") is True), spec.get("tool_review_hash")))
        return self._token(share_id)

    def has_active_public(self):
        now = self._clock()
        with self._db() as db:
            return db.execute("SELECT 1 FROM shares WHERE enabled=1 AND public_enabled=1 AND expires_at>? AND calls<max_calls LIMIT 1",
                              (now,)).fetchone() is not None

    def list_shares(self):
        with self._db() as db:
            rows = db.execute("SELECT * FROM shares ORDER BY created_at DESC").fetchall()
        return [self._decode(row, owner=True) for row in rows]

    def revoke(self, token_or_id):
        share_id = self._id(token_or_id) if "." in token_or_id else token_or_id
        with self._db() as db:
            if db.execute("UPDATE shares SET enabled=0 WHERE id=?", (share_id,)).rowcount != 1:
                raise CapsuleError("This share was not found.", "unavailable", 404)

    def lookup(self, token):
        share_id = self._id(token)
        with self._db() as db:
            row = db.execute("SELECT * FROM shares WHERE id=?", (share_id,)).fetchone()
        if row is None:
            raise CapsuleError("This share link is unavailable.", "unavailable", 404)
        if not row["enabled"] or self._clock() >= row["expires_at"]:
            raise CapsuleError("This link was closed or has expired.", "unavailable", 410)
        return self._decode(row)

    def public_info(self, token):
        """An inactive capability can show a friendly state without exposing tool identity."""
        share_id = self._id(token)
        with self._db() as db:
            row = db.execute("SELECT * FROM shares WHERE id=?", (share_id,)).fetchone()
        if row is None:
            raise CapsuleError("This share link does not exist.", "unavailable", 404)
        return self._decode(row)

    def invoke(self, token, args, *, admit=None):
        share_id, run_id, now = self._id(token), uuid.uuid4().hex, self._clock()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM shares WHERE id=?", (share_id,)).fetchone()
            if row is None:
                raise CapsuleError("This share link does not exist.", "unavailable", 404)
            state = self._status(row)
            if state != "active":
                raise CapsuleError("This link has no runs left." if state == "exhausted" else "This link was closed or has expired.",
                                   state, 429 if state == "exhausted" else 410)
            full_args = self.validated_arguments(row, args)
            if db.execute("SELECT 1 FROM runs WHERE share_id=? AND state IN ('running','queued')", (share_id,)).fetchone():
                raise CapsuleError("This link is already processing a request. Please wait.", "busy", 409)
            if admit is not None and not admit(share_id):
                raise CapsuleError('This link is receiving requests too quickly. Please wait.', 'rate_limited', 429)
            db.execute("UPDATE shares SET calls=calls+1 WHERE id=?", (share_id,))
            db.execute("INSERT INTO runs(id,share_id,state,started_at) VALUES (?,?,'running',?)", (run_id, share_id, now))
            server, tool = row["server"], row["tool"]
        started = time.monotonic()
        try:
            response = self._call_tool(server, tool, full_args)
            if not isinstance(response, dict) or response.get("ok") is not True or response.get("truncated"):
                raise CapsuleError("The tool did not return a complete result. This attempt counts toward the allowance.", "tool_failed", 502)
            result = response.get("structuredContent", response.get("result"))
            if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode()) > MAX_RESULT:
                raise CapsuleError("The result exceeded the delivery limit. This attempt counts toward the allowance.", "result_too_large", 502)
        except BaseException as exc:
            self._finish(run_id, "failed", started)
            if isinstance(exc, CapsuleError):
                raise
            raise CapsuleError("The tool is temporarily unavailable. This attempt counts toward the allowance.", "tool_failed", 502) from exc
        self._finish(run_id, "success", started)
        return result

    @staticmethod
    def validated_arguments(row, args):
        schema, fixed = json.loads(row["schema_json"]), json.loads(row["fixed_json"])
        guest_schema = {**schema, "properties": {k: v for k, v in schema["properties"].items() if k not in fixed},
                        "required": [k for k in schema["required"] if k not in fixed]}
        validate_values(guest_schema, args)
        full_args = {**args, **fixed}
        validate_values(schema, full_args)
        return full_args

    def _finish(self, run_id, state, started):
        with self._db() as db:
            db.execute("UPDATE runs SET state=?,finished_at=?,duration_ms=? WHERE id=?",
                       (state, self._clock(), round((time.monotonic() - started) * 1000), run_id))

    def activity(self):
        with self._db() as db:
            rows = db.execute("SELECT runs.id,runs.state,runs.started_at,runs.duration_ms,shares.title,shares.kind FROM runs JOIN shares ON shares.id=runs.share_id ORDER BY runs.started_at DESC LIMIT 12").fetchall()
            summary = dict(db.execute("SELECT COUNT(*) AS total, SUM(CASE WHEN state='success' THEN 1 ELSE 0 END) AS success FROM runs").fetchone())
        return {"runs": [dict(r) for r in rows], "summary": {k: v or 0 for k, v in summary.items()}}


def serve(manager, port=0):
    # Backwards-compatible guest-only HTTP server used by the original research tests.
    try:
        from .webserver import create_server
    except ImportError:
        from webserver import create_server
    return create_server(manager, lambda: [], port=port, guest_only=True)
