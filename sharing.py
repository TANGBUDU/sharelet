"""Persistent, immutable agent drafts. Only the separate owner API can approve them."""
import hashlib
import hmac
import json
import secrets

try:
    from .sharelet import CapsuleError, _same_json, DEFAULT_LINK_RUNS, DEFAULT_LINK_SECONDS
    from .catalog import reviewed_catalog
except ImportError:
    from sharelet import CapsuleError, _same_json, DEFAULT_LINK_RUNS, DEFAULT_LINK_SECONDS
    from catalog import reviewed_catalog


FIELDS = frozenset({'tool_id', 'title', 'description', 'fixed_args', 'style_notes', 'max_calls', 'expires_seconds', 'public'})


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


class ShareWorkflow:
    def __init__(self, manager, catalog, provider=None):
        self.manager, self.catalog, self.provider = manager, catalog, provider
        with manager._db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS share_drafts (
                id TEXT PRIMARY KEY, state TEXT NOT NULL, body_json TEXT NOT NULL,
                review_json TEXT NOT NULL, review_digest TEXT NOT NULL,
                created_at REAL NOT NULL, expires_at REAL NOT NULL, share_id TEXT UNIQUE)''')

    def prepare(self, body):
        if not isinstance(body, dict) or set(body)-FIELDS:
            raise CapsuleError('Unexpected share settings. Approval flags cannot be supplied by an agent.')
        tool = next((entry for entry in reviewed_catalog(self.catalog())
                     if entry['id'] == body.get('tool_id') and entry.get('supported')
                     and entry.get('safety') != 'blocked'), None)
        if not tool:
            raise CapsuleError('This tool is unavailable or blocked from sharing.', 'tool_unavailable', 409)
        public = body.get('public', False)
        if type(public) is not bool:
            raise CapsuleError('Choose a public link or a local preview.')
        fixed = body.get('fixed_args', {})
        if not isinstance(fixed, dict):
            raise CapsuleError('Invalid fixed parameters.')
        if any(name in fixed and field.get('format')=='sharelet-pdf-uploads' for name,field in tool['schema']['properties'].items()):
            raise CapsuleError('PDF files must be uploaded by the guest for this link.')
        locked = dict(tool.get('locked_args', {}))
        if tool.get('kind') == 'image':
            notes = body.get('style_notes', '')
            if not isinstance(notes, str) or len(notes) > 300:
                raise CapsuleError('Style notes can have at most 300 characters.')
            locked['style_notes'] = notes.strip()
        if any(name in fixed and not _same_json(fixed[name], value) for name, value in locked.items()):
            raise CapsuleError('The reviewed recipe settings are fixed.')
        spec = self.manager.prepare(title=body.get('title'), description=body.get('description', ''),
            server=tool['server'], tool=tool['tool'], tool_id=tool['id'], schema=tool['schema'],
            fixed_args={**fixed, **locked}, max_calls=body.get('max_calls', 5 if tool.get('kind')=='image' else DEFAULT_LINK_RUNS),
            expires_seconds=body.get('expires_seconds', DEFAULT_LINK_SECONDS), kind=tool.get('kind', 'tool'),
            preset=tool.get('style', ''), public_enabled=public)
        return {'share': spec, 'tool_title': tool.get('title', tool['tool']),
                'tool_description': tool.get('description', ''), 'tool_review_hash': tool['review_hash'],
                'requires_side_effects_approval': tool.get('requires_side_effects_approval', True)}

    @staticmethod
    def require_owner_decision(review, body):
        if body.get('public_safe_approved') is not True:
            raise CapsuleError('The owner must review and approve this exact share.', 'owner_approval_required', 403)
        if body.get('tool_review_hash') is not None and body['tool_review_hash'] != review['tool_review_hash']:
            raise CapsuleError('The reviewed tool changed. Review it again.', 'tool_changed', 409)
        if review['requires_side_effects_approval']:
            if (body.get('allow_side_effects') is not True
                    or body.get('tool_review_hash') != review['tool_review_hash']):
                raise CapsuleError('This tool may change data or send messages. It needs an additional owner confirmation.',
                                   'tool_approval_required', 403)

    def create_owned(self, body, public_origin):
        if not isinstance(body, dict) or set(body)-(FIELDS | {'public_safe_approved', 'allow_side_effects', 'tool_review_hash'}):
            raise CapsuleError('Unexpected share settings.')
        review = self.prepare({key: value for key, value in body.items() if key in FIELDS})
        self.require_owner_decision(review, body)
        self.require_ready(review, public_origin)
        spec = dict(review['share'], side_effects_approved=body.get('allow_side_effects') is True,
                    tool_review_hash=review['tool_review_hash'])
        with self.manager._db() as db:
            db.execute('BEGIN IMMEDIATE')
            return self.manager._insert_share(db, spec)

    def require_ready(self, review, origin):
        if review['share']['public_enabled'] and not origin:
            raise CapsuleError('The public address is still opening. Try again in a moment.', 'public_not_ready', 409)
        if review['share']['kind'] == 'image' and self.provider:
            self.provider.require_ready()

    def propose(self, body):
        review = self.prepare(body)
        now, request_id = self.manager._clock(), secrets.token_hex(12)
        with self.manager._db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE share_drafts SET state='expired' WHERE state='pending' AND expires_at<=?", (now,))
            if db.execute("SELECT COUNT(*) FROM share_drafts WHERE state='pending'").fetchone()[0] >= 32:
                raise CapsuleError('Review or reject the existing requests before adding more.', 'draft_limit', 429)
            db.execute("INSERT INTO share_drafts VALUES (?,'pending',?,?,?,?,?,NULL)",
                       (request_id, json.dumps(body), json.dumps(review), digest(review), now, now+3600))
            row = db.execute('SELECT * FROM share_drafts WHERE id=?', (request_id,)).fetchone()
        return self.describe(row)

    def describe(self, row):
        review = json.loads(row['review_json'])
        spec = review['share']
        return {'id': row['id'], 'state': 'expired' if row['state'] == 'pending' and row['expires_at'] <= self.manager._clock() else row['state'],
                'review_digest': row['review_digest'], 'created_at': row['created_at'], 'expires_at': row['expires_at'],
                'tool_title': review['tool_title'], 'tool_description': review['tool_description'],
                'tool_review_hash': review['tool_review_hash'], 'requires_side_effects_approval': review['requires_side_effects_approval'],
                'title': spec['title'], 'description': spec['description'], 'server': spec['server'], 'tool': spec['tool'],
                'fixed_args': spec['fixed_args'], 'guest_fields': [key for key in spec['schema']['properties'] if key not in spec['fixed_args']],
                'max_calls': spec['max_calls'], 'expires_seconds': spec['expires_seconds'], 'public': spec['public_enabled'],
                'kind': spec['kind'], 'share_id': row['share_id']}

    def list_drafts(self, pending=False):
        with self.manager._db() as db:
            rows = db.execute('SELECT * FROM share_drafts ORDER BY created_at DESC LIMIT 100').fetchall()
        drafts = [self.describe(row) for row in rows]
        return [row for row in drafts if row['state'] == 'pending'] if pending else drafts

    def approve(self, draft_id, decision, origin):
        if not isinstance(decision, dict) or set(decision)-{'review_digest', 'public_safe_approved', 'allow_side_effects', 'tool_review_hash'}:
            raise CapsuleError('The proposed share cannot be changed during approval.')
        with self.manager._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM share_drafts WHERE id=?', (draft_id,)).fetchone()
            if not row:
                raise CapsuleError('This request does not exist.', 'unavailable', 404)
            if not isinstance(decision.get('review_digest'), str) or not hmac.compare_digest(row['review_digest'], decision['review_digest']):
                raise CapsuleError('The displayed request changed. Review it again.', 'draft_changed', 409)
            old = json.loads(row['review_json'])
            self.require_owner_decision(old, decision)
            if row['state'] == 'approved':
                return self.manager._token(row['share_id'])
            if row['state'] != 'pending' or row['expires_at'] <= self.manager._clock():
                raise CapsuleError('This request is no longer pending.', 'draft_ended', 409)
            current = self.prepare(json.loads(row['body_json']))
            if digest(current) != row['review_digest']:
                raise CapsuleError('The tool or its rules changed. Request a new draft.', 'tool_changed', 409)
            self.require_ready(current, origin)
            token = self.manager._insert_share(db, dict(current['share'],
                side_effects_approved=decision.get('allow_side_effects') is True, tool_review_hash=current['tool_review_hash']))
            db.execute("UPDATE share_drafts SET state='approved',share_id=? WHERE id=?", (token.split('.')[0], draft_id))
            return token

    def reject(self, draft_id):
        with self.manager._db() as db:
            if db.execute("UPDATE share_drafts SET state='rejected' WHERE id=? AND state='pending'", (draft_id,)).rowcount != 1:
                raise CapsuleError('This request is no longer pending.', 'draft_ended', 409)

    def agent_share_ids(self):
        with self.manager._db() as db:
            return {row[0] for row in db.execute("SELECT share_id FROM share_drafts WHERE state='approved'")}

