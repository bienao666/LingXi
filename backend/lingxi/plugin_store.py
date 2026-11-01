"""Pinned store trust and scoped, signed catalog ingestion. No dialogue leaves Core."""
import json
import time
from typing import Literal
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, Float, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base, SessionLocal, now, uid
from .models import Entity, User
from .web import Context, audit, owned
from .security import encrypt, decrypt, validate_endpoint
from .network import outbound_client
from .plugin_manifest import Manifest, digest
from .plugin_signing import canonical, key_id, MAX_ARTIFACT, sha, verify
from .plugins import PluginRelease, PluginInstallation, invalidate_invocations, owner_lock

router = APIRouter()


class StoreConnection(Entity, Base):
    __tablename__ = 'plugin_store_connections'
    __table_args__ = (UniqueConstraint('user_id','workspace_id'),)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    url: Mapped[str] = mapped_column(String(1000))
    enabled: Mapped[bool] = mapped_column(default=True)
    allow_private: Mapped[bool] = mapped_column(default=False)
    root_keys: Mapped[list] = mapped_column(JSON)
    audience: Mapped[str] = mapped_column(String(100), default='')
    encrypted_token: Mapped[str] = mapped_column(Text, default='')
    version: Mapped[int] = mapped_column(Integer, default=1)
    revision: Mapped[int] = mapped_column(Integer, default=-1)
    trust_epoch: Mapped[int] = mapped_column(Integer, default=0)
    checked_at: Mapped[float] = mapped_column(Float, default=0)
    expires_at: Mapped[float] = mapped_column(Float, default=0)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)


class StoreAccess(Entity, Base):
    __tablename__ = 'plugin_store_access'
    __table_args__ = (UniqueConstraint('user_id','workspace_id','release_id'),)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    connection_id: Mapped[str] = mapped_column(String(64), index=True)
    release_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(30))
    key_id: Mapped[str] = mapped_column(String(64))
    signature: Mapped[dict] = mapped_column(JSON)


class StoreInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: int = Field(ge=0)
    url: str = Field(min_length=1,max_length=1000)
    enabled: bool = True
    allow_private: bool = False
    root_keys: list[str] = Field(min_length=1,max_length=4)
    audience: str = Field(default='',max_length=100)
    token: str | None = Field(default=None,max_length=1000)


class StoreView(BaseModel):
    version: int
    url: str
    enabled: bool
    allow_private: bool
    root_keys: list[str]
    audience: str
    token_configured: bool
    revision: int
    trust_epoch: int
    checked_at: float
    expires_at: float
    error: str | None


def connection(db,user):
    return db.scalar(select(StoreConnection).where(StoreConnection.user_id==user.id, StoreConnection.workspace_id==user.workspace_id))


def view(row):
    return {k:getattr(row,k) for k in StoreView.model_fields if k!='token_configured'}|{'token_configured':bool(row.encrypted_token)}


@router.get('/api/plugin-store',response_model=StoreView|None)
def get_connection(ctx:Context):
    db,user,_=ctx
    row=connection(db,user)
    return view(row) if row else None


@router.put('/api/plugin-store',response_model=StoreView)
def save_connection(body:StoreInput,ctx:Context):
    db,user,_=ctx
    owner_lock(db,user)
    row=connection(db,user)
    if body.version!=(row.version if row else 0):raise HTTPException(409,'商店配置已变化，请刷新')
    url=body.url.strip().rstrip('/')
    validate_endpoint(url,body.allow_private)
    try:
        for pub in body.root_keys:
            import base64
            if len(base64.b64decode(pub,validate=True))!=32:raise ValueError()
    except Exception:raise HTTPException(422,'根公钥须为通过可信渠道获得的 Ed25519 Base64 公钥') from None
    if bool(body.audience)!=bool(body.token or (row and row.encrypted_token and body.token is None)):
        raise HTTPException(422,'私有目录需要工作区标识和只读令牌；公开目录两项留空')
    changed=bool(row and (row.url!=url or row.root_keys!=body.root_keys or row.audience!=body.audience or body.token is not None))
    if not row:
        row=StoreConnection(user_id=user.id,workspace_id=user.workspace_id,url=url,root_keys=body.root_keys)
        db.add(row);db.flush()
    else:row.version+=1
    row.url,row.root_keys,row.audience,row.allow_private,row.enabled=url,body.root_keys,body.audience,body.allow_private,body.enabled
    if body.token is not None:row.encrypted_token=encrypt(body.token) if body.token else ''
    row.checked_at,row.expires_at,row.error=0,0,None
    if changed:
        row.revision,row.trust_epoch=-1,0
        for access in db.scalars(select(StoreAccess).where(StoreAccess.connection_id==row.id)):
            access.status='access_revoked'
        for install in db.scalars(select(PluginInstallation).where(PluginInstallation.user_id==user.id,PluginInstallation.workspace_id==user.workspace_id)):
            if db.get(PluginRelease,install.release_id).source=='store':
                invalidate_invocations(db,user,install.id,'商店信任或连接已变化，请同步核对')
    audit(db,user,'plugin_store_config',row.id)
    return view(row)


def fetch(client,path,limit):
    result=bytearray()
    with client.stream('GET',path) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            result.extend(chunk)
            if len(result)>limit:raise ValueError('Catalog exceeds transport limit')
    return bytes(result)


def verified_feed(data,row):
    bundle=data['trust']; registry=bundle['payload']
    if bundle['root_public_key'] not in row.root_keys:raise ValueError('Unpinned root')
    verify(bundle['root_public_key'],registry,bundle['signature'])
    if registry['format']!='lingxi-trust-v1' or type(registry['epoch']) is not int or registry['epoch']<row.trust_epoch or not 1<=len(registry['keys'])<=100:raise ValueError('Trust rollback')
    keys={}
    for key in registry['keys']:
        if key['key_id']!=key_id(key['public_key']) or key['key_id'] in keys or key['status'] not in ('active','revoked'):raise ValueError('Invalid trust registry')
        if key['status']=='active':keys[key['key_id']]=key['public_key']
    snapshot=data['snapshot']; payload=snapshot['payload']
    verify(keys[snapshot['key_id']],payload,snapshot['signature'])
    if (payload['format']!='lingxi-catalog-v1' or type(payload['revision']) is not int or payload['revision']<row.revision
        or payload['trust_epoch']!=registry['epoch'] or payload['audience']!=row.audience
        or not now()-660<=payload['issued_at']<=now()+30 or not now()<payload['expires_at']<=payload['issued_at']+601
        or len(payload['releases'])>500):raise ValueError('Expired, replayed or wrong audience catalog')
    identities=set()
    for entry in payload['releases']:
        artifact=entry['artifact'];meta=artifact['payload']
        # A revoked signing key invalidates its artifacts without trusting its signature.
        if artifact['key_id'] in keys:verify(keys[artifact['key_id']],meta,artifact['signature'])
        if (entry['id'] in identities or entry['id']!=meta['release_id']
            or entry['id']!=digest({'plugin_id':meta['plugin_id'],'version':meta['version']})
            or entry['status'] not in ('published','withdrawn','revoked') or entry['visibility'] not in ('public','private')
            or type(meta['size']) is not int or not 1<=meta['size']<=MAX_ARTIFACT):raise ValueError('Invalid catalog entry')
        import re
        if not re.fullmatch('[a-f0-9]{64}',meta['digest']):raise ValueError('Invalid digest')
        identities.add(entry['id'])
    return payload,keys,registry['epoch']


def sync(identity):
    with SessionLocal() as db:
        row=db.get(StoreConnection,identity)
        if not row or not row.enabled:raise HTTPException(409,'尚未启用商店连接')
        config={k:getattr(row,k) for k in ('url','allow_private','encrypted_token','version')}
        with outbound_client(config['url'],allow_private=config['allow_private'],timeout=15,
            headers={'Authorization':'Bearer '+decrypt(config['encrypted_token'])} if config['encrypted_token'] else {}) as client:
            data=json.loads(fetch(client,'store/v1/feed',3*1024*1024))
            payload,keys,epoch=verified_feed(data,row)
            # Apply known revocations before downloading anything. A failed object
            # must not prevent signed revocations from taking effect.
            with SessionLocal.begin() as writer:
                user=writer.get(User,row.user_id);owner_lock(writer,user)
                current=writer.get(StoreConnection,identity,populate_existing=True)
                if current.version!=config['version']:raise ValueError('Configuration changed')
                if current.revision>payload['revision'] or current.trust_epoch>epoch:raise ValueError('Snapshot superseded')
                current.revision,current.trust_epoch=payload['revision'],epoch
                incoming={e['id']:e for e in payload['releases']}
                for access in writer.scalars(select(StoreAccess).where(StoreAccess.connection_id==identity)):
                    entry=incoming.get(access.release_id)
                    access.status=('revoked' if entry['artifact']['key_id'] not in keys else entry['status']) if entry else 'access_revoked'
                    if access.status in ('revoked','access_revoked'):
                        for install in writer.scalars(select(PluginInstallation).where(PluginInstallation.user_id==user.id,PluginInstallation.workspace_id==user.workspace_id)):
                            if install.release_id==access.release_id or access.release_id in install.dependency_lock.values():
                                invalidate_invocations(writer,user,install.id,'插件版本或签名信任已撤销，结果未提交')
            fetched=[];started=time.monotonic();total=0
            for entry in payload['releases']:
                meta=entry['artifact']['payload']
                if entry['artifact']['key_id'] not in keys or entry['status']=='revoked':continue
                if time.monotonic()-started>60:raise ValueError('Catalog sync budget exceeded')
                existing=db.get(PluginRelease,entry['id'])
                if existing:
                    if existing.source!='store' or existing.digest!=meta['digest']:raise ValueError('Immutable release conflict')
                    blob=canonical(existing.manifest)
                else:blob=fetch(client,'store/v1/artifacts/'+meta['digest'],MAX_ARTIFACT)
                total+=len(blob)
                if total>16*1024*1024:raise ValueError('Catalog artifact budget exceeded')
                if len(blob)!=meta['size'] or sha(blob)!=meta['digest']:raise ValueError('Artifact digest mismatch')
                manifest=Manifest.model_validate_json(blob).model_dump()
                if canonical(manifest)!=blob or manifest['plugin_id']!=meta['plugin_id'] or manifest['version']!=meta['version'] or manifest['publisher']=='lingxi' or any(c['kind']=='native' for c in manifest['capabilities']):raise ValueError('Invalid artifact contract')
                fetched.append((entry,manifest))
    with SessionLocal.begin() as db:
        user=db.get(User,row.user_id);owner_lock(db,user)
        current=db.get(StoreConnection,identity,populate_existing=True)
        if current.version!=config['version'] or current.revision!=payload['revision'] or current.trust_epoch!=epoch:raise ValueError('Sync superseded')
        for entry,manifest in fetched:
            meta=entry['artifact']['payload']
            release=db.get(PluginRelease,entry['id'])
            if not release:
                release=PluginRelease(id=entry['id'],plugin_id=manifest['plugin_id'],version=manifest['version'],manifest=manifest,digest=meta['digest'],source='store',status='published')
                db.add(release)
            elif release.source!='store' or release.digest!=meta['digest']:raise ValueError('Immutable release conflict')
            access=db.scalar(select(StoreAccess).where(StoreAccess.user_id==user.id,StoreAccess.workspace_id==user.workspace_id,StoreAccess.release_id==release.id))
            if not access:
                access=StoreAccess(user_id=user.id,workspace_id=user.workspace_id,connection_id=identity,release_id=release.id,status=entry['status'],key_id=entry['artifact']['key_id'],signature=entry['artifact'])
                db.add(access)
            else:access.status,access.key_id,access.signature=entry['status'],entry['artifact']['key_id'],entry['artifact']
        current.checked_at,current.expires_at,current.error=now(),payload['expires_at'],None
        audit(db,user,'plugin_store_sync',identity)


def sync_safe(identity):
    try:sync(identity)
    except Exception:
        with SessionLocal.begin() as db:
            row=db.get(StoreConnection,identity)
            if row:
                row.error='商店同步失败，请检查连接、令牌、固定公钥、签名与制品；已有有效安装可继续，安装和更新暂停。'
                from .operations import event
                event(db,'store_sync_failed','store',workspace=row.workspace_id,reference=row.id)
        raise HTTPException(503,'商店同步失败，未安装或更新；请检查连接、签名和制品状态') from None


@router.post('/api/plugin-store/sync',response_model=StoreView)
def synchronize(ctx:Context):
    db,user,_=ctx
    row=connection(db,user)
    if not row:raise HTTPException(409,'请先配置独立商店')
    identity=row.id
    db.commit()
    sync_safe(identity)
    db.expire_all()
    return view(db.get(StoreConnection,identity))


def access(db,user,release,fresh=False):
    if release.source!='store':return None
    row=db.scalar(select(StoreAccess).where(StoreAccess.user_id==user.id,StoreAccess.workspace_id==user.workspace_id,StoreAccess.release_id==release.id))
    if not row or row.status not in ('published','withdrawn'):raise HTTPException(409,'商店版本已撤销或不在当前工作区可访问范围')
    if fresh:
        conn=db.get(StoreConnection,row.connection_id)
        if row.status!='published' or not conn.enabled or conn.error or conn.expires_at<=now():raise HTTPException(503,'安装或更新需要有效的新商店快照，请先同步目录')
    return row


def visible_releases(db,user):
    result=[]
    for release in db.scalars(select(PluginRelease)):
        if release.source=='store':
            try:
                row=access(db,user,release)
                if row.status!='published':continue
            except HTTPException:continue
        result.append(release)
    return result


def tick():
    with SessionLocal() as db:
        ids=list(db.scalars(select(StoreConnection.id).where(StoreConnection.enabled==True,StoreConnection.checked_at<now()-300)))
    for identity in ids:
        try:sync_safe(identity)
        except HTTPException:pass
