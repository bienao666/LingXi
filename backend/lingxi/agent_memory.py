"""Untrusted child proposals enter the canonical review queue, never the fact store."""
from fastapi import HTTPException
from sqlalchemy import select
from .models import Candidate, Memory
from .tasks import digest
from .web import owned, view


def roots_valid(db, provenance, seen=None):
    from .resources import valid as resource_valid
    if any(not resource_valid(db, ref) for ref in provenance.get('resources', [])):
        return False
    from .memory import usable
    from .media import Media
    for ref in provenance.get('memories', []):
        memory=db.get(Memory,ref['id'],populate_existing=True)
        if (not usable(memory, seen=seen) or memory.version!=ref['version'] or
            memory.user_id!=provenance.get('user_id') or memory.workspace_id!=provenance.get('workspace_id')):
            return False
    for ref in provenance.get('media', []):
        item=db.get(Media,ref['id'],populate_existing=True)
        from .server_documents import source_valid
        if (not item or not source_valid(db, item) or item.status!='ready' or item.version!=ref['version'] or item.user_id!=ref['user_id'] or
            item.workspace_id!=provenance.get('workspace_id')):
            return False
    return True


def validate(db, candidate, lock=False):
    from .delegation import AgentRun, AgentNode
    from .agent_worker import validate_sources, validate_profiles
    origin=candidate.origin
    run=db.get(AgentRun,origin.get('run_id'),populate_existing=True)
    if not run or run.user_id!=candidate.user_id or run.workspace_id!=candidate.workspace_id:
        raise HTTPException(410,'候选协作来源不存在或超出范围')
    validate_sources(db,run,lock=lock)
    if lock:
        run=db.scalar(select(AgentRun).where(AgentRun.id==run.id).with_for_update().execution_options(populate_existing=True))
        validate_sources(db,run,lock=False)
    validate_profiles(db,run)
    node=db.get(AgentNode,origin.get('node_id'),populate_existing=True)
    if (not run.config.get('propose_memory') or run.config.get('qq_task_id') or run.cancel_requested or
        run.status not in ('completed','partial') or run.round!=origin.get('round') or
        not node or node.run_id!=run.id or node.status!='succeeded' or
        digest(node.result)!=origin.get('result_digest') or not roots_valid(db,origin)):
        raise HTTPException(410,'候选来源、执行结果或授权已失效，不能保存')
    return run,node


def preview(db, candidate):
    validate(db,candidate)
    return {'candidate':view(candidate),'review_digest':digest({'id':candidate.id,'content':candidate.content,
        'category':candidate.category,'subject':candidate.subject,'origin':candidate.origin}),
        'warning':'这是子助手提出的待核对内容，不是已核验事实。确认表示你核对并采纳此项；不会自动保存同任务的其他候选。'}


def publish(db, run, nodes):
    """Called in the fenced summary transaction; retries use the immutable node identity."""
    if not run.config.get('propose_memory') or run.config.get('qq_task_id'):
        return
    from .chat_actions import for_target
    from .media import Media
    action=for_target(db,run.id)
    for node in nodes:
        if node.status!='succeeded' or node.capability!='reason':
            continue
        for proposal in node.result.get('memory_candidates',[]):
            fp=digest({k:proposal[k] for k in ('content','subject','category')})
            if db.scalar(select(Candidate.id).where(Candidate.source_id==node.id,Candidate.fingerprint==fp)):
                continue
            context=node.context
            sources={'task-goal':{'id':'task-goal','kind':'task_goal','content':run.goal}}
            sources.update({m['id']:{'kind':'memory',**m} for m in context.get('memories',[])})
            sources.update({m['id']:{'kind':'media',**m} for m in context.get('attachments',[])})
            sources.update({d['id']:{'kind':'dependency',**d} for d in context.get('dependencies',[])})
            origin={'kind':'agent','run_id':run.id,'node_id':node.id,'node_key':node.key,'round':run.round,
                'user_id':run.user_id,'workspace_id':run.workspace_id,
                'result_digest':digest(node.result),'agent_name':node.config['name'],
                'model':node.config['model'],'model_id':node.config['model_id'],
                'provider_version':node.config['provider_version'],
                'persona':{k:node.config['persona'][k] for k in ('id','name','version')},
                'role':{k:node.config['role'][k] for k in ('id','name','version')},
                'plugin_id':node.config.get('plugin_id'),'plugin_version':node.config.get('plugin_version'),
                'plugin_release_id':node.config.get('plugin_release_id'),
                'plugin_role':node.config.get('plugin_role'),
                'source_message_ids':[action.source_id] if action else [],
                'memories':context.get('memory_provenance',[]),
                'resources':[run.config['resource_policy']] if run.config.get('allow_files') else [],
                'media':[{**ref,'user_id':db.get(Media,ref['id']).user_id} for ref in context.get('media_provenance',[])],
                'citations':[sources[i] for i in proposal['evidence_ids']]}
            db.add(Candidate(user_id=run.user_id,workspace_id=run.workspace_id,source_id=node.id,
                fingerprint=fp,content=proposal['content'],category=proposal['category'],subject=proposal['subject'],
                evidence='agent_proposal',reason=proposal['reason'],status='pending',origin=origin))


def invalidate_pending(db, run_id):
    for candidate in db.scalars(select(Candidate).where(Candidate.status=='pending')):
        if candidate.origin.get('kind')=='agent' and candidate.origin.get('run_id')==run_id:
            candidate.status,candidate.content='invalidated',''
            candidate.origin={k:v for k,v in candidate.origin.items() if k in ('kind','run_id','node_id','round')}


def expire(db, user):
    for candidate in db.scalars(select(Candidate).where(Candidate.user_id==user.id,
            Candidate.workspace_id==user.workspace_id,Candidate.status=='pending')):
        if candidate.origin.get('kind')=='agent':
            try:
                validate(db,candidate)
            except HTTPException:
                invalidate_pending(db,candidate.origin['run_id'])


def invalidate_derived(db, kind, identity):
    """Delete derived facts on data removal; plugin removal alone doesn't delete user assets."""
    def matches(provenance):
        return any(r['id']==identity for r in provenance.get(kind,[]))
    from .memory import MemoryService
    for memory in db.scalars(select(Memory).where(Memory.status!='deleted')):
        if matches(memory.provenance):
            MemoryService().forget(db,memory)
        elif any(matches(revision.get('provenance',{})) for revision in memory.revisions):
            memory.revisions=[{**r,'content':'','provenance':{}} if matches(r.get('provenance',{})) else r for r in memory.revisions]


def sources_for(db, candidate, user):
    owned(db,Candidate,candidate.id,user)
    if candidate.origin.get('kind')=='agent':
        return preview(db,candidate)
    raise HTTPException(404,'此候选不是协作来源')
