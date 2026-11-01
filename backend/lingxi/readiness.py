"""Read-only readiness evidence. Never probe paid models or turn a check into a grant."""
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select

from .db import now
from .models import Model, Policy, Provider
from .web import Context

router = APIRouter()
State = Literal['configured', 'observed', 'online', 'unconfigured', 'disabled', 'attention', 'unknown']
LIMIT = 20


class ReadinessItem(BaseModel):
    id: str
    name: str
    state: State
    message: str
    observed_at: float | None = None


class ReadinessGroup(BaseModel):
    id: str
    name: str
    href: str
    items: list[ReadinessItem]
    total: int
    truncated: bool = False
    note: str


class ReadinessAlert(BaseModel):
    code: str
    title: str
    message: str
    href: str


class ReadinessView(BaseModel):
    checked_at: float
    alerts: list[ReadinessAlert]
    groups: list[ReadinessGroup]
    note: str


def item(identity, name, state, message, at=None):
    return ReadinessItem(id=identity, name=name, state=state, message=message, observed_at=at)


def local_models(db, user):
    from .providers import protocol_for, require_capability
    entries = []
    for purpose, name in (('chat','默认对话'),('extraction','记忆抽取'),('embedding','记忆嵌入'),('rerank','检索重排')):
        policy = db.get(Policy, purpose)
        model = db.get(Model, policy.model_id) if policy else None
        provider = db.get(Provider, model.provider_id) if model else None
        # Do not disclose identifiers or names from another workspace's policy.
        if not model or model.workspace_id != user.workspace_id or model.purpose != purpose or not provider or provider.workspace_id != user.workspace_id:
            entries.append(item(purpose,name,'unconfigured','默认用途尚无当前工作区的有效模型配置'));continue
        if not provider.enabled:
            entries.append(item(purpose,name,'disabled','服务连接已停用'));continue
        try: require_capability(protocol_for(provider, db), purpose)
        except HTTPException:
            entries.append(item(purpose,name,'attention','连接协议不支持此用途或配置无效'));continue
        if model.status != 'verified' or model.verified_provider_version != provider.version:
            entries.append(item(purpose,name,'attention','模型尚未验证，或连接变更后需要重新验证'));continue
        entries.append(item(purpose,name,'configured','当前连接版本已验证；远端实时可用性未探测',model.verified_at))
    return ReadinessGroup(id='models',name='模型配置',href='#/models/policies',items=entries,total=4,
        note='仅核对默认固定用途。自动路由、人格覆盖和具体任务仍按各自策略选择，不从本快照推断可执行性。')


def bounded(db, entity, user, *extra):
    conditions = (entity.workspace_id == user.workspace_id, entity.user_id == user.id, *extra)
    query = select(entity).where(*conditions)
    rows = list(db.scalars(query.order_by(entity.created_at.desc(),entity.id).limit(LIMIT)))
    total = db.scalar(select(func.count()).select_from(entity).where(*conditions))
    return rows, total


def channel_states(db, user, at):
    from .channels import Channel
    rows,total = bounded(db,Channel,user)
    entries = []
    for row in rows:
        if not row.enabled: state,message='disabled','实例已停用'
        elif not row.connection_id or row.lease_until <= at: state,message='attention','认证协议连接离线或租约已到期'
        elif row.login_state == 'online': state,message='online','协议端最近报告在线；不代表消息已送达或用户已读'
        elif row.login_state == 'login_required': state,message='attention','协议端报告需要人工处理登录；不会自动循环登录'
        else: state,message='unknown','协议已连接，登录状态仍待确认'
        entries.append(item(row.id,row.name,state,message,row.last_seen))
    return ReadinessGroup(id='channels',name='平台连接',href='#/channels/instances',items=entries,total=total,
        truncated=total>len(rows),note='依据持久连接租约与协议心跳，不发测试消息，不探测真实 QQ 账号。')


def plugin_states(db, user):
    from .plugins import PluginInstallation, release_checked
    rows,total = bounded(db,PluginInstallation,user,PluginInstallation.status != 'uninstalled')
    entries = []
    for row in rows:
        if row.status != 'enabled':
            entries.append(item(row.id,row.plugin_id,'disabled','安装未启用'));continue
        try:
            release = release_checked(db,row.release_id,user=user)
            if release.plugin_id != row.plugin_id:
                raise ValueError('Installation and release identity differ')
        except (HTTPException,ValueError,TypeError,KeyError):
            entries.append(item(row.id,row.plugin_id,'attention','当前版本不可访问、已撤销或清单校验失败'));continue
        entries.append(item(row.id,row.plugin_id,'observed','安装已启用、当前清单校验通过；未证明依赖、连接或调用可用',row.updated_at))
    return ReadinessGroup(id='plugins',name='插件状态',href='#/plugins/installed',items=entries,total=total,
        truncated=total>len(rows),note='这是启停与当前清单快照。依赖锁、工具授权、账号及具体能力须在插件中心核对，实际调用仍重新检查。')


def voice_states(db,user):
    from .voice import VoiceConfig,VoiceSettings,checked
    row=db.get(VoiceConfig,user.id)
    if row and row.workspace_id != user.workspace_id:row=None
    invalid = False
    if row:
        try: VoiceSettings.model_validate(row.spec)
        except (ValueError,TypeError): invalid = True
    entries=[]
    for kind,name in (('asr','语音识别'),('tts','语音合成'),('realtime_asr','实时转写'),('realtime_tts','实时合成')):
        if invalid:state,message='attention','保存的语音配置格式异常，请在语音配置中核对'
        elif not row or not row.spec.get('enabled'):state,message='disabled','语音未启用；不会开启麦克风或播放'
        elif not row.spec.get(kind,{}).get('model'):state,message='unconfigured','此语音用途尚未配置'
        elif checked(db,row,kind):state,message='configured','当前配置验证有效；实际音质、设备及延迟未在此探测'
        else:state,message='attention','验证缺失或连接／音色／语音配置变化后需重新验证'
        entries.append(item(kind,name,state,message))
    return ReadinessGroup(id='voice',name='语音配置',href='#/settings/voice',items=entries,total=4,
        note='语音开关和历史验证不等于正在录音、实时会话有效或物理设备可用。')


def store_state(db,user,at):
    from .plugin_store import connection
    row=connection(db,user)
    if not row:state,message='unconfigured','未连接独立商店，不影响第一方能力'
    elif not row.enabled:state,message='disabled','商店连接已停用'
    elif row.error:state,message='attention','最近目录同步失败，请在插件中心核对；不展示远端错误正文'
    elif row.expires_at<=at:state,message='attention','签名目录快照已到期，新安装与更新不可用'
    else:state,message='observed','签名目录快照仍在有效期；未证明商店服务当前在线'
    return ReadinessGroup(id='store',name='商店目录',href='#/plugins/catalog',
        items=[item('store','商店同步',state,message,row.checked_at if row and row.checked_at else None)],total=1,
        note='只读取本地同步结果，不同步目录、不接收对话或记忆；已安装且授权有效的插件按原规则运行。')


@router.get('/api/admin/operations/readiness',response_model=ReadinessView)
def readiness(ctx:Context):
    from .operations import overview
    db,user,_=ctx
    at=now()
    groups=[local_models(db,user),channel_states(db,user,at),plugin_states(db,user),voice_states(db,user),store_state(db,user,at)]
    runtime=overview(db,user)
    alerts=[]
    def alert(code,title,message,href):alerts.append(ReadinessAlert(code=code,title=title,message=message,href=href))
    if runtime['worker']['status']!='ready':
        alert('worker_unavailable','后台离线','45 秒内未收到 worker 心跳；后台任务和提醒可能无法继续。','#/settings/overview')
    if runtime['oldest_ready_seconds']>=300:
        alert('queue_backlog','队列积压','已有到期作业等待至少五分钟；不把未来预约作业计入积压。','#/data/logs')
    if runtime['disk']['status']!='ready':
        alert('disk_'+runtime['disk']['status'],'存储异常','运行卷空间偏低或容量暂不可读；不会自动删除文件。','#/settings/media')
    for group in groups:
        bad=sum(entry.state in ('attention','unknown') or (group.id=='models' and entry.state in ('disabled','unconfigured')) for entry in group.items)
        if bad:
            alert(group.id+'_attention',group.name,f'本次已检查条目中有 {bad} 项需要核对；以分项依据及时间为准。',group.href)
    return ReadinessView(checked_at=at,groups=groups,alerts=alerts,
        note='只读证据快照，不调用模型、远程插件或 QQ，不修改配置、重试任务、切换渠道或授予权限。无提示不代表全部功能或真实质量已经验收。')
