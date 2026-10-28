import hashlib
import json
import os
from pathlib import Path
from urllib.parse import quote
import httpx
from sqlalchemy import select
from .config import settings
from .db import SessionLocal, now, uid
from .models import Candidate, ChatRun, Conversation, Job, Memory, Message, Operation, Policy, RunEvent

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
        with httpx.Client(timeout=60, follow_redirects=False, trust_env=False, headers=self.headers) as client:
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


def usable(memory):
    return memory and memory.status == 'active' and (not memory.expires_at or memory.expires_at > now())


class MemoryService:
    def __init__(self, adapter=None):
        self.adapter = adapter or HindsightAdapter()

    def search(self, db, user_id, query):
        # Ignore all framework-generated prose; only use validated document references.
        result = self.adapter.recall(user_id, query)
        documents = {x.get('document_id') for x in result.get('results', [])}
        memories = db.scalars(select(Memory).where(Memory.user_id == user_id, Memory.workspace_id == 'personal',
                                                   Memory.status == 'active')).all()
        return [m for m in memories if usable(m) and m.index_status == 'ready' and m.index_epoch == index_epoch(db) and doc_id(m) in documents][:12]

    def remember(self, db, user_id, content, category, source_ids, **extra):
        memory = Memory(user_id=user_id, content=content, category=category, source_ids=source_ids, **extra)
        db.add(memory)
        db.flush()
        enqueue(db, f'index:{memory.id}:1', 'index', {'memory_id': memory.id, 'version': 1})
        return memory

    def correct(self, db, memory, values):
        old_doc = doc_id(memory)
        memory.revisions = memory.revisions + [{'version': memory.version, 'content': memory.content,
                            'source_ids': memory.source_ids, 'recorded_at': now(), 'status': 'superseded'}]
        memory.version += 1
        for key, value in values.items():
            setattr(memory, key, value)
        memory.evidence = 'explicit'
        memory.source_ids = memory.source_ids + [f'user-correction:{memory.version}']
        memory.index_status = 'pending'
        invalidate_runs(db, memory.id)
        for source_id in memory.source_ids:
            source = db.get(Message, source_id)
            if source and source.status == 'completed':
                source.status = 'superseded'
                invalidate_source(db, source_id)
        for candidate in db.scalars(select(Candidate).where(Candidate.memory_id == memory.id)):
            candidate.status = 'invalidated'
        enqueue(db, f'index:{memory.id}:{memory.version}', 'index', {'memory_id': memory.id, 'version': memory.version})
        enqueue(db, f'cleanup:{old_doc}', 'cleanup', {'user_id': memory.user_id, 'documents': [old_doc], 'epochs': list(range(0, index_epoch(db)+1))})

    def forget(self, db, memory, journal=True):
        if journal:
            append_ledger({'kind': 'memory', 'id': memory.id, 'user_id': memory.user_id})
        memory.status = 'deleted'
        memory.index_status = 'deleting'
        memory.content = ''
        documents = [f'{memory.id}-v{v}' for v in range(1, memory.version + 1)]
        memory.revisions = []
        invalidate_runs(db, memory.id)
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
        operation = Operation(user_id=memory.user_id, kind='delete', target_id=memory.id)
        db.add(operation)
        db.flush()
        enqueue(db, f'delete:{operation.id}', 'cleanup', {'user_id': memory.user_id, 'documents': documents,
                                                       'operation_id': operation.id, 'memory_id': memory.id,
                                                       'epochs': list(range(0, index_epoch(db)+1))})
        return operation


def invalidate_runs(db, memory_id):
    # Lock order: worker validates versions before every persisted chunk and every final response.
    for run in db.scalars(select(ChatRun).where(ChatRun.status.in_(['queued', 'running', 'completed']))):
        if any(ref['id'] == memory_id for ref in run.memory_refs):
            invalidate_run(db, run)


def invalidate_source(db, source_id):
    for run in db.scalars(select(ChatRun)):
        if source_id in run.source_message_ids:
            invalidate_run(db, run)


def invalidate_run(db, run, seen=None):
    seen = seen if seen is not None else set()
    if run.id in seen:
        return
    seen.add(run.id)
    run.cancel_requested = True
    run.status = 'invalidated'
    run.error = '依据已纠正或删除，请重新提问'
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
    # Export results are generated on download, never restored as stale content blobs.
    return count


def delete_conversation(db, conversation, delete_memories, journal=True):
    if journal:
        append_ledger({'kind': 'conversation', 'id': conversation.id, 'user_id': conversation.user_id,
                       'delete_memories': delete_memories})
    sources = set(db.scalars(select(Message.id).where(Message.conversation_id == conversation.id)))
    for candidate in db.scalars(select(Candidate).where(Candidate.source_id.in_(sources))):
        candidate.status, candidate.content = 'invalidated', ''
    if delete_memories:
        for memory in db.scalars(select(Memory).where(Memory.user_id == conversation.user_id, Memory.status != 'deleted')):
            if sources.intersection(memory.source_ids):
                MemoryService().forget(db, memory, journal=journal)
    conversation.deleted = True
    conversation.title = '已删除对话'
    for run in db.scalars(select(ChatRun).where(ChatRun.conversation_id == conversation.id)):
        run.cancel_requested, run.status, run.error = True, 'invalidated', '对话已删除'
        db.query(RunEvent).filter(RunEvent.run_id == run.id).delete()
    for message in db.scalars(select(Message).where(Message.conversation_id == conversation.id)):
        message.content, message.status = '', 'deleted'


def fingerprint(content):
    return hashlib.sha256(content.strip().encode()).hexdigest()
