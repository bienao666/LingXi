"""Task-scoped attachment references; bytes are expanded only for their assigned model."""
import json
from fastapi import HTTPException
from sqlalchemy import select
from .media import Media, validate_attachments, verify_vision, content_for_items


def select_media(db, ids, user):
    items = validate_attachments(db, ids, user, None, None)
    if any(m.parsed.get('kind') not in ('image', 'document') for m in items):
        raise HTTPException(422, '协作附件仅支持已解析文档和图片，语音请先确认转写后以文字提交')
    by_id = {m.id:m for m in items}
    return [by_id[identity] for identity in ids]


def refs_for(items):
    return [{'id':m.id, 'version':m.version} for m in items]


def load(db, run, refs=None, lock=False):
    from .agent_worker import AgentStopped
    actor_id = run.user_id
    if run.config.get('qq_task_id'):
        from .channel_tasks import require_origin
        try:
            task = require_origin(db, run)
            actor_id = task.actor_id
        except HTTPException:
            raise AgentStopped('QQ 附件来源或任务授权已失效') from None
    result = {}
    references = run.config.get('media_refs', []) if refs is None else refs
    # Lock in stable ID order, but retain the user's selected order in model context.
    for ref in sorted(references, key=lambda r:r['id']):
        query = select(Media).where(Media.id == ref['id']).execution_options(populate_existing=True)
        item = db.scalar(query.with_for_update() if lock else query)
        from .server_documents import source_valid
        if (not item or not source_valid(db, item) or item.status != 'ready' or item.version != ref['version'] or
                item.user_id != actor_id or item.workspace_id != run.workspace_id or
                item.parsed.get('kind') not in ('document', 'image')):
            raise AgentStopped('协作附件已删除、变化或超出授权范围')
        result[item.id] = item
    return [result[ref['id']] for ref in references]


def describe(items):
    return [{'id':m.id, 'version':m.version, 'filename':m.filename,
             'kind':m.parsed['kind'], 'parts_count':len(m.parsed.get('parts', []))} for m in items]


def vision(db, run, config, items):
    if any(m.parsed['kind'] == 'image' for m in items):
        from .agent_worker import model_for
        model, provider = model_for(db, run, config)
        if not verify_vision(db, model, provider):
            raise HTTPException(409, '被分配图片的子助手模型尚未通过当前连接的视觉验证，未丢弃图片或切换模型')


def validate_plan(db, run, plan, agents):
    items = {m.id:m for m in load(db, run)}
    covered = set()
    for node in plan.nodes:
        ids = node.media_ids
        if len(ids) != len(set(ids)) or not set(ids) <= items.keys():
            raise HTTPException(422, '规划包含重复附件或本任务范围外附件')
        if ids and node.capability != 'reason':
            raise HTTPException(422, '附件只可分配给分析节点；远程工具须通过分析依赖和逐次预览确认')
        chosen = agents.get(node.agent_id)
        if not chosen:
            raise HTTPException(409, '规划选择了未知助手')
        vision(db, run, chosen, [items[i] for i in ids])
        covered.update(ids)
    if covered != items.keys():
        raise HTTPException(422, '规划未覆盖全部提交附件，未执行节点；请调整目标后重新提交')


def node_content(db, run, node, data):
    refs = node.config.get('media_refs', [])
    items = load(db, run, refs)
    vision(db, run, node.config, items)
    content, _ = content_for_items(json.dumps(data, ensure_ascii=False), items)
    return content


def text_budget(messages):
    """Image bytes have a separate bounded transport budget; charge a conservative image allowance."""
    total = 0
    for message in messages:
        content = message.get('content', '')
        if isinstance(content, str):
            total += len(content.encode('utf-8'))
        else:
            for part in content:
                total += 8000 if part.get('type') == 'image_url' else len(part.get('text', '').encode('utf-8'))
    return total


def coverage(db, run, nodes):
    return [m | {'nodes':[{'key':n.key, 'status':n.status} for n in nodes
                           if m['id'] in {r['id'] for r in n.config.get('media_refs', [])}]}
            for m in describe(load(db, run))]
