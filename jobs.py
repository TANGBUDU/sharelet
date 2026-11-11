"""Durable tool/image jobs with idempotent admission and separate paid-image limits."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import re
import secrets
import time
from pathlib import Path

try:
    from .sharelet import CapsuleError, DispatchNotSent, dispatch_scope, MAX_RESULT
    from .media import unwrap_result
except ImportError:
    from sharelet import CapsuleError, DispatchNotSent, dispatch_scope, MAX_RESULT
    from media import unwrap_result


class ImageJobs:
    def __init__(self, manager, media, *, require_ready=lambda: None, require_tool=None,
                 limit=30, execution_limit=1000, workers=4, queue_limit=16, files=None):
        self.manager, self.media, self.require_ready = manager, media, require_ready
        self.limit, self.workers = limit, workers
        self.queue_limit=max(workers,queue_limit)
        self.require_tool, self.execution_limit = require_tool, execution_limit
        self.files=files
        self.limits_path=Path(manager.root)/'limits.json'
        if self.limits_path.exists():
            saved=json.loads(self.limits_path.read_text(encoding='utf-8'))
            self._validate_limits(saved)
            self.limit,self.execution_limit=saved['generation_limit'],saved['execution_limit']
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sharelet-image")
        with manager._db() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS image_jobs(
                id TEXT PRIMARY KEY, share_id TEXT NOT NULL, request_key TEXT NOT NULL,
                fingerprint TEXT NOT NULL, state TEXT NOT NULL, created_at REAL NOT NULL,
                finished_at REAL, error TEXT, width INTEGER, height INTEGER,
                UNIQUE(share_id,request_key)
              );
              CREATE TABLE IF NOT EXISTS generation_budget(id INTEGER PRIMARY KEY CHECK(id=1), reserved INTEGER NOT NULL);
              INSERT OR IGNORE INTO generation_budget(id,reserved) VALUES(1,0);
              CREATE TABLE IF NOT EXISTS execution_budget(id INTEGER PRIMARY KEY CHECK(id=1), reserved INTEGER NOT NULL);
            """)
            db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in db.execute("PRAGMA table_info(image_jobs)")}
            for name, definition in (("kind", "TEXT NOT NULL DEFAULT 'image'"),
                                     ("result_json", "TEXT"),
                                     ("artifact_purged", "INTEGER NOT NULL DEFAULT 0")):
                if name not in columns:
                    db.execute("ALTER TABLE image_jobs ADD COLUMN " + name + " " + definition)
            db.execute("INSERT OR IGNORE INTO execution_budget(id,reserved) SELECT 1,COUNT(*) FROM image_jobs")
            db.execute("CREATE INDEX IF NOT EXISTS jobs_state ON image_jobs(state)")
            db.execute("CREATE INDEX IF NOT EXISTS runs_share_state ON runs(share_id,state)")
            db.execute("CREATE INDEX IF NOT EXISTS runs_started ON runs(started_at)")

    def recover_interrupted(self):
        """Only the process holding the profile's service lease calls this at boot."""
        with self.manager._db() as db:
            db.execute("UPDATE image_jobs SET state='interrupted',finished_at=?,error=? WHERE state IN ('queued','running')",
                       (self.manager._clock(), "The creator's service restarted. This request was not automatically resubmitted."))
            db.execute("UPDATE runs SET state='interrupted' WHERE state IN ('queued','running')")

    @staticmethod
    def _validate_limits(body):
        if (not isinstance(body,dict) or set(body)!={'generation_limit','execution_limit'}
                or type(body['generation_limit']) is not int or not 0<=body['generation_limit']<=1000
                or type(body['execution_limit']) is not int or not 0<=body['execution_limit']<=100000):
            raise CapsuleError('Choose total run limits from 0–100,000 and image limits from 0–1,000.')

    def set_limits(self,body):
        self._validate_limits(body)
        try:
            from .image_provider import atomic_private
        except ImportError:
            from image_provider import atomic_private
        with self.manager._db() as db:
            db.execute('BEGIN IMMEDIATE')
            used=db.execute('SELECT reserved FROM execution_budget WHERE id=1').fetchone()[0]
            images=db.execute('SELECT reserved FROM generation_budget WHERE id=1').fetchone()[0]
            if body['execution_limit']<used or body['generation_limit']<images:
                raise CapsuleError('A limit cannot be lower than its recorded attempts. The counters are never reset.')
            atomic_private(self.limits_path,body)
            self.limit,self.execution_limit=body['generation_limit'],body['execution_limit']
        return {'usage':self.usage(),'budget':self.budget()}

    def budget(self):
        with self.manager._db() as db:
            reserved = db.execute("SELECT reserved FROM generation_budget WHERE id=1").fetchone()[0]
            completed = db.execute("SELECT COUNT(*) FROM image_jobs WHERE state='succeeded' AND kind='image'").fetchone()[0]
        return {"limit": self.limit, "used": reserved, "remaining": max(0, self.limit - reserved), "completed": completed}

    def usage(self):
        with self.manager._db() as db:
            used = db.execute("SELECT reserved FROM execution_budget WHERE id=1").fetchone()[0]
            completed = db.execute("SELECT COUNT(*) FROM image_jobs WHERE state='succeeded'").fetchone()[0]
        return {"limit": self.execution_limit, "used": used, "remaining": max(0, self.execution_limit-used),
                "completed": completed}

    def submit(self, token, args, request_key, *, admit=None):
        if not isinstance(request_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{20,80}", request_key):
            raise CapsuleError("A valid request identifier is required.")
        share_id = self.manager._id(token)
        if not isinstance(args, dict):
            raise CapsuleError("Enter a character description.")
        fingerprint = hashlib.sha256(json.dumps(args, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()).hexdigest()
        now = self.manager._clock()
        with self.manager._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM shares WHERE id=?", (share_id,)).fetchone()
            if row is None:
                raise CapsuleError("This link is unavailable.", "unavailable", 404)
            previous = db.execute("SELECT * FROM image_jobs WHERE share_id=? AND request_key=?", (share_id, request_key)).fetchone()
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise CapsuleError("This request identifier was already used for a different input.", "request_conflict", 409)
                if not row["enabled"] or now >= row["expires_at"]:
                    raise CapsuleError("This link has ended.", "unavailable", 410)
                return self._public(previous)
            state = self.manager._status(row)
            if state != "active":
                raise CapsuleError("This link has no generations left." if state == "exhausted" else "This link has ended.", state, 429 if state == "exhausted" else 410)
            full_args = self.manager.validated_arguments(row, args)
            try:
                from .file_inputs import file_fields
            except ImportError:
                from file_inputs import file_fields
            identifiers=[]
            if file_fields(json.loads(row['schema_json'])):
                if not self.files:raise CapsuleError('File uploads are unavailable.','unavailable',503)
                identifiers=self.files.validate_inputs(db,row,full_args)
            if self.require_tool:
                self.require_tool(row)
            if row["kind"] == "image":
                self.require_ready()
            if db.execute("SELECT COUNT(*) FROM image_jobs WHERE state IN ('queued','running')").fetchone()[0] >= self.queue_limit:
                raise CapsuleError("The service is busy. Please try again in a moment. No run was charged.", "busy", 409)
            if db.execute("SELECT 1 FROM image_jobs WHERE share_id=? AND state IN ('queued','running')", (share_id,)).fetchone():
                raise CapsuleError("This link is already running a request. Please wait for it to finish.", "busy", 409)
            reserved = db.execute("SELECT reserved FROM generation_budget WHERE id=1").fetchone()[0]
            if row["kind"] == "image" and reserved >= self.limit:
                raise CapsuleError("The creator's generation allowance has been reached.", "profile_budget", 429)
            executions = db.execute("SELECT reserved FROM execution_budget WHERE id=1").fetchone()[0]
            if executions >= self.execution_limit:
                raise CapsuleError("The creator's total run allowance has been reached.", "execution_budget", 429)
            if admit is not None and not admit(share_id):
                raise CapsuleError('This link is receiving requests too quickly. Please wait a moment.', 'rate_limited', 429)
            job_id = secrets.token_hex(16)
            if identifiers:self.files.lock_inputs(db,identifiers,job_id)
            if row["kind"] == "image":
                db.execute("UPDATE generation_budget SET reserved=reserved+1 WHERE id=1")
            db.execute("UPDATE execution_budget SET reserved=reserved+1 WHERE id=1")
            db.execute("UPDATE shares SET calls=calls+1 WHERE id=?", (share_id,))
            db.execute("INSERT INTO image_jobs(id,share_id,request_key,fingerprint,state,created_at,kind) VALUES(?,?,?,?,'queued',?,?)", (job_id,share_id,request_key,fingerprint,now,row["kind"]))
            db.execute("INSERT INTO runs(id,share_id,state,started_at) VALUES(?,?,'queued',?)", (job_id,share_id,now))
            server, tool, kind = row["server"], row["tool"], row["kind"]
        try:
            self.executor.submit(self._execute, job_id, server, tool, full_args, kind)
        except RuntimeError:
            self._complete(job_id, "interrupted", now, error="The creator's service stopped. No automatic resubmission was made.")
        return self.get(token, job_id)

    @staticmethod
    def _public(row):
        value = {key: row[key] for key in ("id", "state", "created_at", "finished_at", "error", "width", "height", "kind")}
        if row["state"] == "succeeded" and row["kind"] == "tool" and row["result_json"] is not None:
            value["result"] = json.loads(row["result_json"])
        return value

    def get(self, token, job_id):
        share_id = self.manager._id(token)
        info = self.manager.public_info(token)
        if info["status"] in ("expired", "revoked"):
            raise CapsuleError("This link has ended.", "unavailable", 410)
        with self.manager._db() as db:
            row = db.execute("SELECT * FROM image_jobs WHERE id=? AND share_id=?", (job_id,share_id)).fetchone()
        if row is None:
            raise CapsuleError("Request not found.", "unavailable", 404)
        return self._public(row)

    def by_request(self, token, request_key):
        share_id = self.manager._id(token)
        with self.manager._db() as db:
            row = db.execute("SELECT id FROM image_jobs WHERE share_id=? AND request_key=?", (share_id, request_key)).fetchone()
        if row is None:
            raise CapsuleError("No run was admitted for this request.", "request_not_found", 404)
        return self.get(token, row[0])

    def cleanup_media(self):
        with self.manager._db() as db:
            rows = db.execute("SELECT image_jobs.id FROM image_jobs JOIN shares ON shares.id=image_jobs.share_id WHERE image_jobs.artifact_purged=0 AND image_jobs.state NOT IN ('queued','running') AND (shares.enabled=0 OR shares.expires_at<=?)", (self.manager._clock(),)).fetchall()
        for row in rows:
            self.media.path(row[0]).unlink(missing_ok=True)
            if self.files:self.files.purge(row[0])
        if rows:
            with self.manager._db() as db:
                db.executemany("UPDATE image_jobs SET result_json=NULL,artifact_purged=1 WHERE id=?", [(row[0],) for row in rows])
        if self.files:self.files.cleanup()

    def _check_dispatch(self, job_id):
        with self.manager._db() as db:
            row = db.execute('SELECT shares.* FROM shares JOIN image_jobs ON shares.id=image_jobs.share_id WHERE image_jobs.id=?', (job_id,)).fetchone()
        if not row or not row['enabled'] or row['expires_at'] <= self.manager._clock():
            raise DispatchNotSent('This share ended before the tool was called. No run was charged.', 'share_ended_before_dispatch')
        if self.require_tool:
            try:
                self.require_tool(row)
            except CapsuleError as error:
                raise DispatchNotSent(str(error), error.code) from None

    def _execute(self, job_id, server, tool, args, kind):
        started = time.monotonic()
        with self.manager._db() as db:
            row=db.execute('SELECT shares.enabled,shares.expires_at FROM shares JOIN image_jobs ON shares.id=image_jobs.share_id WHERE image_jobs.id=?',(job_id,)).fetchone()
            if not row or not row['enabled'] or row['expires_at']<=self.manager._clock():
                self._complete(job_id,'interrupted',started,refund=True,
                               error='This share ended before the tool was called. No run was charged.')
                return
            db.execute("UPDATE image_jobs SET state='running' WHERE id=?", (job_id,))
            db.execute("UPDATE runs SET state='running' WHERE id=?", (job_id,))
        try:
            with dispatch_scope(lambda: self._check_dispatch(job_id)):
                result = self.manager._call_tool(server, tool, args)
            if isinstance(result,dict) and result.get('not_sent') is True and result.get('ok') is not True:
                self._complete(job_id,'failed',started,refund=True,
                               error='The service is busy. This request was not sent to the tool, and no run was charged. Please try again.')
                return
            if isinstance(result,dict) and result.get('uncertain') is True:
                self._complete(job_id,'interrupted',started,error='The tool did not return before its deadline. It may still finish remotely. This attempt was not automatically retried.')
                return
            if not isinstance(result, dict) or result.get("ok") is not True or result.get("truncated"):
                raise CapsuleError("The tool did not return a complete result.")
            value = result.get("structuredContent", result.get("result"))
            if kind == "image":
                metadata = self.media.save(job_id, value)
                if not self._complete(job_id, "succeeded", started, metadata=metadata):
                    self.media.path(job_id).unlink(missing_ok=True)
            else:
                value = unwrap_result(value)
                if self.files and isinstance(value,dict) and value.get('kind')=='file_error':
                    try:
                        from .file_inputs import FILE_ERRORS
                    except ImportError:
                        from file_inputs import FILE_ERRORS
                    code=value.get('code')
                    if code in FILE_ERRORS:raise CapsuleError(FILE_ERRORS[code],code)
                    raise CapsuleError('The tool could not process the PDF.')
                if self.files and isinstance(value,dict) and value.get('kind')=='file':
                    value=self.files.save(job_id,value)
                encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
                if len(encoded.encode()) > MAX_RESULT:
                    raise CapsuleError("The tool result exceeded the delivery limit.")
                if not self._complete(job_id, "succeeded", started, result_json=encoded):
                    if self.files and isinstance(value,dict) and value.get('kind')=='file':self.files.purge(job_id)
        except DispatchNotSent as error:
            state = 'interrupted' if error.code == 'share_ended_before_dispatch' else 'failed'
            self._complete(job_id, state, started, refund=True, error=str(error))
        except CapsuleError as error:
            try:
                from .file_inputs import FILE_ERRORS
            except ImportError:
                from file_inputs import FILE_ERRORS
            message=str(error) if error.code in FILE_ERRORS else "The request did not complete. The attempt counts toward the link's allowance; it was not automatically retried."
            self._complete(job_id,'failed',started,error=message)
        except Exception:
            self._complete(job_id, "failed", started, error="The request did not complete. The attempt counts toward the link's allowance; it was not automatically retried.")
        finally:
            if self.files:self.files.finish_inputs(job_id)

    def _complete(self, job_id, state, started, *, error=None, metadata=None, result_json=None, refund=False):
        now, metadata = self.manager._clock(), metadata or {}
        with self.manager._db() as db:
            if refund:
                db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE image_jobs SET state=?,finished_at=?,error=?,width=?,height=?,result_json=? WHERE id=? AND state IN ('queued','running')", (state,now,error,metadata.get("width"),metadata.get("height"),result_json,job_id)).rowcount
            if not changed:
                return False
            if refund:
                # Only for attempts proven never sent to the tool. Same transaction as the state change,
                # so a request can be refunded at most once.
                job = db.execute("SELECT share_id,kind FROM image_jobs WHERE id=?", (job_id,)).fetchone()
                db.execute("UPDATE shares SET calls=calls-1 WHERE id=? AND calls>0", (job["share_id"],))
                db.execute("UPDATE execution_budget SET reserved=reserved-1 WHERE id=1 AND reserved>0")
                if job["kind"] == "image":
                    db.execute("UPDATE generation_budget SET reserved=reserved-1 WHERE id=1 AND reserved>0")
            db.execute("UPDATE runs SET state=?,finished_at=?,duration_ms=? WHERE id=?", ("success" if state=="succeeded" else "failed" if state=="failed" else "interrupted",now,max(0,round((time.monotonic()-started)*1000)),job_id))
        return True

    def close(self):
        self.executor.shutdown(wait=False, cancel_futures=True)
