import hashlib
import json
import os
from pathlib import Path
from urllib.parse import quote
import httpx
from sqlalchemy import select
from .config import settings
from .db import SessionLocal, now, uid
from .models import Candidate, ChatRun, Conversation, Job, Memory, Message, Operation, Policy, RunEvent, User
from .network import outbound_client

CATEGORIES = {'profile', 'preferences', 'experiences', 'projects', 'relationships', 'procedures', 'support'}


def enqueue(db, key: str, kind: str, payload: dict):
    if not db.scalar(select(Job).where(Job.key == key)):
        db.add(Job(key=key, kind=kind, payload=payload))


class HindsightAdapter:
    """REST adapter; document IDs bind retrieved data to canonical memory versions."""
    def __init__(self):
        self.url = settings().hindsight_url.rstrip('/')
        self.headers = {'Authorization': 'Bearer ' + settings().hindsight_api_key} if settings().hindsight_api_key else {}

    def request(self, method, path, **kwargs):
        with outbound_client(self.url, allow_private=True, timeout=60, headers=self.headers) as client:
            response = client.request(method, self.url + path, **kwargs)
            response.raise_for_status()
            return response.json() if response.content else {}

    def bank(self, user_id, epoch=None):
        if epoch is None:
            with SessionLocal() as db:
                epoch = index_epoch(db)
        return '/v1/default/banks/' + quote(f'lingxi-personal-{user_id}-e{epoch}', safe='')

    def retain(self, user_id, document_id, content, epoch=None):
        return self.request('POST', self.bank(user_id, epoch) + '/memories', json={
            'items': [{'content': content, 'document_id': document_id}], 'async': False})

    def recall(self, user_id, query):
        return self.request('POST', self.bank(user_id) + '/memories/recall', json={
            'query': query, 'types': ['world', 'experience'], 'budget': 'low', 'max_tokens': 2500, 'include': {'entities': {'max_tokens': 0}}})

    def delete(self, user_id, document_id, epoch=None):
        try:
            self.request('DELETE', self.bank(user_id, epoch) + '/documents/' + quote(document_id, safe=''))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise

    def healthy(self):
        try:
            self.request('GET', '/health/ready')
            return True
        except Exception:
            return False


def doc_id(memory):
    return f'{memory.id}-v{memory.version}'


def index_epoch(db):
    policy = db.get(Policy, 'embedding')
    return policy.version if policy else 0


def usable(memory, seen=None):
    if not memory or memory.status != 'active' or (memory.expires_at and memory.expires_at <= now()):
        return False
    if memory.provenance:
        from sqlalchemy.orm import object_session
        from .agent_memory import roots_valid
        seen=set(seen or ())
        if memory.id in seen:
            return False
        seen.add(memory.id)
        db=object_session(memory)
        if db is None or not roots_valid(db,memory.provenance,seen):
            return False
    return True


class MemoryService:
    def __init__(self, adapter=None):
        self.adapter = adapter or HindsightAdapter()

    def search(self, db, user_id, query):
        # Ignore all framework-generated prose; only use validated document references.
        user = db.get(User, user_id)
        if not user:
            return []
        result = self.adapter.recall(user_id, query)
        rows = result.get('results') if isinstance(result, dict) else None
        if not isinstance(rows, list) or len(rows) > 5000:
            raise ValueError('Invalid memory recall response')
        documents, identities, seen = [], set(), set()
        for row in rows:
            document = row.get('document_id') if isinstance(row, dict) else None
            if not isinstance(document, str) or len(document) > 100 or document in seen:
                continue
            identity, separator, version = document.rpartition('-v')
            if not separator or not identity or len(identity) > 64 or not version.isascii() or not version.isdecimal():
                continue
            seen.add(document)
            documents.append(document)
            identities.add(identity)
        epoch = index_epoch(db)
        # Keep both SQL parameters and ORM materialization bounded for large banks.
        by_document = {}
        ordered_ids = sorted(identities)
        for start in range(0, len(ordered_ids), 400):
            memories = db.scalars(select(Memory).where(Memory.id.in_(ordered_ids[start:start+400]),
                Memory.user_id == user_id, Memory.workspace_id == user.workspace_id,
                Memory.status == 'active', Memory.index_status == 'ready', Memory.index_epoch == epoch))
            by_document.update((doc_id(m), m) for m in memories if usable(m))
        # Recall order is the framework's relevance order; database order is unrelated.
        return [by_document[document] for document in documents if document in by_document][:12]

    def remember(self, db, user_id, content, category, source_ids, **extra):
        memory = Memory(user_id=user_id, content=content, category=category, source_ids=source_ids, **extra)
        db.add(memory)
        db.flush()
        enqueue(db, f'index:{memory.id}:1', 'index', {'memory_id': memory.id, 'version': 1})
        from .proactive import automatic_topic
        automatic_topic(db, memory)
        return memory

    def submit_agent_candidates(self, db, run, nodes):
        from .agent_memory import publish
        publish(db,run,nodes)

    def correct(self, db, memory, values):
        old_doc = doc_id(memory)
        memory.revisions = memory.revisions + [{'version': memory.version, 'content': memory.content,
                            'source_ids': memory.source_ids, 'provenance':memory.provenance,
                            'subject': memory.subject, 'category': memory.category,
                            'event_at': memory.event_at, 'valid_from': memory.valid_from, 'valid_to': memory.valid_to,
                            'recorded_at': now(), 'status': 'superseded'}]
        memory.version += 1
        for key, value in values.items():
            setattr(memory, key, value)
        memory.evidence = 'explicit'
        memory.provenance = {}
        memory.source_ids = memory.source_ids + [f'user-correction:{memory.version}']
        memory.index_status = 'pending'
        invalidate_runs(db, memory.id)
        from .agent_memory import invalidate_derived
        invalidate_derived(db,'memories',memory.id)
        for source_id in memory.source_ids:
            source = db.get(Message, source_id)
            if source and source.status == 'completed':
                source.status = 'superseded'
                invalidate_source(db, source_id)
        for candidate in db.scalars(select(Candidate).where(Candidate.memory_id == memory.id)):
            candidate.status = 'invalidated'
            if candidate.origin:
                candidate.content=''
                candidate.origin={k:v for k,v in candidate.origin.items() if k in ('kind','run_id','node_id','round')}
        enqueue(db, f'index:{memory.id}:{memory.version}', 'index', {'memory_id': memory.id, 'version': memory.version})
        enqueue(db, f'cleanup:{old_doc}', 'cleanup', {'user_id': memory.user_id, 'documents': [old_doc], 'epochs': list(range(0, index_epoch(db)+1))})

    def forget(self, db, memory, journal=True):
        if journal:
            append_ledger({'kind': 'memory', 'id': memory.id, 'user_id': memory.user_id})
        memory.status = 'deleted'
        memory.index_status = 'deleting'
        memory.content = ''
        memory.subject = 'unknown'
        memory.event_at = memory.valid_from = memory.valid_to = None
        documents = [f'{memory.id}-v{v}' for v in range(1, memory.version + 1)]
        memory.revisions = []
        memory.provenance = {}
        invalidate_runs(db, memory.id)
        from .agent_memory import invalidate_derived
        invalidate_derived(db,'memories',memory.id)
        # Remove original dialogue evidence too; pending extraction must not resurrect it.
        source_ids = set(s for s in memory.source_ids if ':' not in s)
        for source in db.scalars(select(Message).where(Message.id.in_(source_ids))):
            source.status, source.content = 'redacted', ''
            invalidate_source(db, source.id)
        for candidate in db.scalars(select(Candidate).where(Candidate.source_id.in_(source_ids))):
            candidate.status, candidate.content = 'invalidated', ''
        for run in db.scalars(select(ChatRun)):
            if source_ids.intersection(run.source_message_ids):
                invalidate_run(db, run)
        for candidate in db.scalars(select(Candidate).where(Candidate.memory_id == memory.id)):
            candidate.status, candidate.content = 'invalidated', ''
            candidate.origin={k:v for k,v in candidate.origin.items() if k in ('kind','run_id','node_id','round')}
        operation = Operation(user_id=memory.user_id, kind='delete', target_id=memory.id)
        db.add(operation)
        db.flush()
        enqueue(db, f'delete:{operation.id}', 'cleanup', {'user_id': memory.user_id, 'documents': documents,
                                                       'operation_id': operation.id, 'memory_id': memory.id,
                                                       'epochs': list(range(0, index_epoch(db)+1))})
        return operation


def invalidate_runs(db, memory_id):
    from .delegation import invalidate_delegations
    invalidate_delegations(db, memory_id=memory_id)
    from .proactive import invalidate_topics
    invalidate_topics(db, 'memory', memory_id)
    # Lock order: worker validates versions before every persisted chunk and every final response.
    for run in db.scalars(select(ChatRun).where(ChatRun.status.in_(['queued', 'running', 'completed']))):
        if any(ref['id'] == memory_id for ref in run.memory_refs):
            invalidate_run(db, run)


def invalidate_source(db, source_id):
    from .channel_tasks import invalidate as invalidate_tasks
    invalidate_tasks(db, source_id=source_id)
    from .emotions import invalidate_sources
    invalidate_sources(db, source_id)
    for run in db.scalars(select(ChatRun)):
        if source_id in run.source_message_ids:
            invalidate_run(db, run)


def invalidate_run(db, run, seen=None):
    seen = seen if seen is not None else set()
    if run.id in seen:
        return
    seen.add(run.id)
    from .conversation_context import invalidate as invalidate_context
    invalidate_context(db, run.id)
    from .chat_actions import invalidate as invalidate_action
    invalidate_action(db, run.id)
    run.cancel_requested = True
    run.status = 'invalidated'
    run.error = '依据已纠正或删除，请重新提问'
    if run.config.get('task_assist'):
        run.config = run.config | {'task_assist': run.config['task_assist'] | {'status': 'invalidated'}}
    from .channels import ChannelInbound, ChannelDelivery
    incoming = db.scalar(select(ChannelInbound).where(ChannelInbound.run_id == run.id))
    if incoming:
        incoming.status, incoming.error = 'cancelled', '回答依据或身份授权已失效'
        for delivery in db.scalars(select(ChannelDelivery).where(ChannelDelivery.inbound_id == incoming.id)):
            delivery.content = ''
            delivery.encrypted_audio = None
            if delivery.status == 'pending':
                delivery.status, delivery.error = 'cancelled', incoming.error
    db.query(RunEvent).filter(RunEvent.run_id == run.id).delete()
    replies = list(db.scalars(select(Message).where(Message.run_id == run.id, Message.role == 'assistant')))
    for message in replies:
        message.content, message.status = '', 'invalidated'
    reply_ids = {m.id for m in replies}
    if reply_ids:
        for later in db.scalars(select(ChatRun)):
            if reply_ids.intersection(later.source_message_ids):
                invalidate_run(db, later, seen)


def append_ledger(entry):
    directory = settings().runtime_dir
    directory.mkdir(parents=True, exist_ok=True)
    # A separate volume must be retained independently when restoring an old database backup.
    data = (json.dumps(entry, ensure_ascii=False) + '\n').encode()
    with open(directory / 'deletions.jsonl', 'ab', buffering=0) as file:
        file.write(data)
        os.fsync(file.fileno())


def replay_ledger(db):
    ledger = settings().runtime_dir / 'deletions.jsonl'
    if not ledger.exists():
        return 0
    count = 0
    for line in ledger.read_text(encoding='utf-8').splitlines():
        item = json.loads(line)  # Corrupt ledger blocks startup rather than silently resurrecting data.
        if item['kind'] == 'memory':
            memory = db.get(Memory, item['id'])
            if memory and memory.status != 'deleted':
                MemoryService().forget(db, memory, journal=False)
                count += 1
        elif item['kind'] == 'conversation':
            conversation = db.get(Conversation, item['id'])
            if conversation:
                delete_conversation(db, conversation, item.get('delete_memories', False), journal=False)
        elif item['kind'] == 'media':
            from .media import Media, forget_media
            media = db.get(Media, item['id'])
            if media and media.status != 'deleted':
                forget_media(db, media, journal=False)
        elif item['kind'] == 'emotion':
            from .emotions import EmotionObservation, discard
            row = db.get(EmotionObservation, item['id'])
            if row and row.user_id == item['user_id'] and row.version <= item['max_version']:
                discard(db, row)
        elif item['kind'] == 'support':
            from .assistants import SupportPreference
            from .emotions import invalidate_preferences
            invalidate_preferences(db, item['user_id'], item['max_version'])
            pref = db.get(SupportPreference, item['user_id'])
            if pref and pref.version <= item['max_version']:
                pref.spec = pref.spec | {'emotion_enabled': False, 'auto_observe': False,
                    'self_report': '', 'report_expires_at': None}
                pref.version = item['max_version'] + 1
    # Export results are generated on download, never restored as stale content blobs.
    return count


def delete_conversation(db, conversation, delete_memories, journal=True):
    if journal:
        append_ledger({'kind': 'conversation', 'id': conversation.id, 'user_id': conversation.user_id,
                       'delete_memories': delete_memories})
    from .delegation import invalidate_delegations
    invalidate_delegations(db, conversation_id=conversation.id)
    sources = set(db.scalars(select(Message.id).where(Message.conversation_id == conversation.id)))
    from .media import Media, MediaLink, forget_media
    # Media is a user asset: remove only attachments no longer used by another live conversation.
    media_ids = list(db.scalars(select(MediaLink.media_id).where(MediaLink.message_id.in_(sources))).unique())
    for media_id in media_ids:
        other = db.scalar(select(MediaLink.id).join(Message, Message.id == MediaLink.message_id)
                          .join(Conversation, Conversation.id == Message.conversation_id)
                          .where(MediaLink.media_id == media_id, Conversation.id != conversation.id, Conversation.deleted == False).limit(1))
        item = db.get(Media, media_id)
        if not other and item and item.status != 'deleted':
            forget_media(db, item, journal=journal)
    from .proactive import invalidate_topics
    invalidate_topics(db, conversation_id=conversation.id)
    for candidate in db.scalars(select(Candidate).where(Candidate.source_id.in_(sources))):
        candidate.status, candidate.content = 'invalidated', ''
    if delete_memories:
        for memory in db.scalars(select(Memory).where(Memory.user_id == conversation.user_id, Memory.status != 'deleted')):
            if sources.intersection(memory.source_ids):
                MemoryService().forget(db, memory, journal=journal)
    conversation.deleted = True
    conversation.title = '已删除对话'
    for run in db.scalars(select(ChatRun).where(ChatRun.conversation_id == conversation.id)):
        from .conversation_context import invalidate as invalidate_context
        invalidate_context(db, run.id)
        from .chat_actions import invalidate as invalidate_action
        invalidate_action(db, run.id)
        run.cancel_requested, run.status, run.error = True, 'invalidated', '对话已删除'
        if run.config.get('task_assist'):
            run.config = run.config | {'task_assist': run.config['task_assist'] | {'status': 'invalidated'}}
        db.query(RunEvent).filter(RunEvent.run_id == run.id).delete()
    for message in db.scalars(select(Message).where(Message.conversation_id == conversation.id)):
        message.content, message.status = '', 'deleted'
        # Forks reference these IDs; their queued and completed answers must expire too.
        invalidate_source(db, message.id)


def fingerprint(content):
    return hashlib.sha256(content.strip().encode()).hexdigest()
