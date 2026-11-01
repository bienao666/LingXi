"""Authenticated PersonaService management and per-conversation selection."""
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from .db import now
from .models import Conversation
from .web import Context, audit, owned
from .assistants import (AssistantProfile, ProfileRevision, ConversationPersona, SupportPreference,
    PersonaInput, RoleInput, AgentInput, ProfileView, RevisionView, PersonaSelection,
    SelectionView, SupportUpdate, SupportView, profile_owned, validate_spec,
    record_revision, serialize, support_view)

router = APIRouter()


def register_profiles(path, kind, schema_class):
    def listing(ctx: Context):
        db, user, _ = ctx
        items = db.scalars(select(AssistantProfile).where(AssistantProfile.user_id == user.id,
            AssistantProfile.workspace_id == user.workspace_id, AssistantProfile.kind == kind)
            .order_by(AssistantProfile.created_at, AssistantProfile.id))
        return [serialize(db, item, user) for item in items]

    def create(body: schema_class, ctx: Context):
        db, user, _ = ctx
        if body.version != 0:
            raise HTTPException(409, '新配置的版本应为 0')
        spec = validate_spec(db, body, user, kind)
        item = AssistantProfile(user_id=user.id, workspace_id=user.workspace_id, kind=kind,
            name=body.name, description=body.description, enabled=body.enabled, spec=spec)
        db.add(item)
        db.flush()
        record_revision(db, item)
        audit(db, user, f'{kind}_create', item.id)
        return serialize(db, item, user)

    def update(identity: str, body: schema_class, ctx: Context):
        db, user, _ = ctx
        item = profile_owned(db, identity, user, kind)
        db.refresh(item, with_for_update=True)
        if item.version != body.version:
            raise HTTPException(409, '配置已变化，请重新加载后编辑')
        spec = validate_spec(db, body, user, kind)
        item.name, item.description, item.enabled = body.name, body.description, body.enabled
        item.spec, item.version, item.updated_at = spec, item.version + 1, now()
        record_revision(db, item)
        audit(db, user, f'{kind}_update', item.id)
        return serialize(db, item, user)

    def history(identity: str, ctx: Context, before: int | None = Query(None, ge=1)):
        db, user, _ = ctx
        profile_owned(db, identity, user, kind)
        query = select(ProfileRevision).where(ProfileRevision.profile_id == identity,
            ProfileRevision.user_id == user.id, ProfileRevision.workspace_id == user.workspace_id)
        if before:
            query = query.where(ProfileRevision.version < before)
        return [{'version': r.version, 'created_at': r.created_at, 'snapshot': r.snapshot}
            for r in db.scalars(query.order_by(ProfileRevision.version.desc()).limit(50))]

    def copy(identity: str, ctx: Context):
        db, user, _ = ctx
        source = profile_owned(db, identity, user, kind)
        spec = validate_spec(db, schema_class(name=source.name, enabled=False, **source.spec), user, kind)
        item = AssistantProfile(user_id=user.id, workspace_id=user.workspace_id, kind=kind,
            name=(source.name[:95] + ' · 副本'), description=source.description, enabled=False, spec=spec)
        db.add(item)
        db.flush()
        record_revision(db, item)
        audit(db, user, f'{kind}_copy', item.id)
        return serialize(db, item, user)

    router.add_api_route(path, listing, methods=['GET'], response_model=list[ProfileView], name=f'list_{kind}')
    router.add_api_route(path, create, methods=['POST'], response_model=ProfileView, name=f'create_{kind}')
    router.add_api_route(path+'/{identity}', update, methods=['PUT'], response_model=ProfileView, name=f'update_{kind}')
    router.add_api_route(path+'/{identity}/revisions', history, methods=['GET'], response_model=list[RevisionView], name=f'history_{kind}')
    router.add_api_route(path+'/{identity}/copy', copy, methods=['POST'], response_model=ProfileView, name=f'copy_{kind}')


register_profiles('/api/personas', 'persona', PersonaInput)
register_profiles('/api/agent-roles', 'role', RoleInput)
register_profiles('/api/agent-profiles', 'agent', AgentInput)


@router.get('/api/conversations/{conversation_id}/persona', response_model=SelectionView)
def selection(conversation_id: str, ctx: Context):
    db, user, _ = ctx
    owned(db, Conversation, conversation_id, user)
    row = db.get(ConversationPersona, conversation_id)
    from .system_settings import current
    inherited = not row or row.inherit_default
    identity = current(db, user).config.default_persona_id if inherited else row.persona_id
    persona = profile_owned(db, identity, user, 'persona') if identity else None
    return SelectionView(persona_id=row.persona_id if row else None, version=row.version if row else 0,
        inherit_default=inherited, persona_name=persona.name if persona else '并肩伙伴', persona_version=persona.version if persona else 0)


@router.put('/api/conversations/{conversation_id}/persona', response_model=SelectionView)
def choose_persona(conversation_id: str, body: PersonaSelection, ctx: Context):
    db, user, _ = ctx
    owned(db, Conversation, conversation_id, user, lock=True)
    if body.inherit_default and body.persona_id:
        raise HTTPException(422, '继承默认人格时不能同时指定会话人格')
    from .system_settings import current
    identity = current(db, user).config.default_persona_id if body.inherit_default else body.persona_id
    persona = profile_owned(db, identity, user, 'persona', enabled=True) if identity else None
    row = db.get(ConversationPersona, conversation_id)
    if (row.version if row else 0) != body.version:
        raise HTTPException(409, '会话人格已变化，请重新加载')
    if row:
        row.persona_id, row.version = body.persona_id, row.version + 1
        row.inherit_default = body.inherit_default
    else:
        row = ConversationPersona(conversation_id=conversation_id, persona_id=body.persona_id, inherit_default=body.inherit_default)
        db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, '会话人格已变化，请重新加载') from None
    audit(db, user, 'conversation_persona_change', conversation_id)
    return SelectionView(persona_id=row.persona_id, version=row.version,
        inherit_default=row.inherit_default, persona_name=persona.name if persona else '并肩伙伴', persona_version=persona.version if persona else 0)


@router.get('/api/support-preferences', response_model=SupportView)
def get_support(ctx: Context):
    db, user, _ = ctx
    return support_view(db.get(SupportPreference, user.id))


@router.put('/api/support-preferences', response_model=SupportView)
def update_support(body: SupportUpdate, ctx: Context):
    db, user, _ = ctx
    from .support import save_support
    return save_support(db, user, body)
