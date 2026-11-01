import copy
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from lingxi.api import app
from lingxi.db import SessionLocal, now
from lingxi.models import User
from lingxi import configuration_history as h
from lingxi.model_budgets import ModelDispatch
from lingxi.operations import OpsPreference
from lingxi.security import decrypt, encrypt

BASE='/api/admin/configuration-bundles'
HISTORY='/api/admin/configuration-revisions'


def apply(client, names=None, **changes):
    state=client.get(BASE).json()
    values=state['document']['modules']
    values['workspace']['workspace_name']='历史合成空间'
    values['media']['max_files']=3
    values['operations']['log_retention_days']=30
    values['model_budget'].update(daily_limit=10,execution_limit=3)
    for name, fields in changes.items():values[name].update(fields)
    names=names or list(values)
    state['document']['modules']={k:values[k] for k in names}
    state['versions']={k:state['versions'][k] for k in names}
    p=client.post(BASE+'/previews',json=state)
    assert p.status_code==200,p.text
    r=confirm(client,p.json());assert r.status_code==200,r.text
    return r.json()


def confirm(client, preview):
    return client.put(BASE,json={'token':preview['token'],'acknowledgement':'atomic_configuration_changes'})


def rows(client):
    r=client.get(HISTORY);assert r.status_code==200,r.text
    return r.json()['items']


def rollback(client, row, names=None, point='before'):
    names=names or row['modules']
    versions=client.get(BASE).json()['versions']
    return client.post(f"{HISTORY}/{row['id']}/previews",json={
        'modules':names,'point':point,'versions':{n:versions[n] for n in names}})


def test_atomic_bundle_has_one_complete_revision_and_immutable_source(client):
    initial=client.get(BASE).json();assert rows(client)==[]
    final=apply(client);records=rows(client);assert len(records)==1
    r=records[0];assert r['action']=='import' and r['number']==1 and set(r['modules'])==set(h.NAMES)
    detail=client.get(f"{HISTORY}/{r['id']}").json()
    assert detail['before_snapshot']==initial and detail['after_snapshot']==final
    assert not {'user_id','workspace_id','before_snapshot'} & set(r)
    assert client.put(f"{HISTORY}/{r['id']}",json={}).status_code==405
    assert client.delete(f"{HISTORY}/{r['id']}").status_code==405


@pytest.mark.parametrize('name',['workspace','media','operations','model_budget'])
def test_individual_management_page_edits_share_history(client,name):
    current=client.get(BASE).json();values=current['document']['modules'][name]
    paths={'workspace':'system-settings','media':'media-policy','model_budget':'model-budgets'}
    if name=='operations':
        r=client.put('/api/admin/operations/preferences',json={'version':0,'log_retention_days':45,'daily_token_alert':10})
    else:
        if name=='workspace':
            values['workspace_name']='独立页面';body={'version':current['versions'][name],'action':'edit','config':values};ack='new_turns_only'
        elif name=='media':values['max_files']=4;body={'version':current['versions'][name],**values};ack='new_submissions_only'
        else:values['daily_limit']=12;body={'version':current['versions'][name],**values};ack='request_attempts_not_money'
        base='/api/admin/'+paths[name]
        p=client.post(base+'/previews',json=body);assert p.status_code==200,p.text
        r=client.put(base,json={'token':p.json()['token'],'acknowledgement':ack})
    assert r.status_code==200,r.text
    records=rows(client);assert len(records)==1 and records[0]['action']=='edit' and records[0]['modules']==[name]


def test_scoped_rollback_restores_values_but_keeps_counts_and_other_modules(client):
    initial=client.get(BASE).json();after=apply(client);record=rows(client)[0]
    with SessionLocal.begin() as db:
        db.add(ModelDispatch(id='count-must-stay',workspace_id='personal',provider_id='synthetic',root_kind='background',root_id='',policy_version=1))
    p=rollback(client,record,['media','model_budget']);assert p.status_code==200,p.text
    assert p.json()['action']=='rollback' and p.json()['source_id']==record['id']
    assert {c['module'] for c in p.json()['changes']}=={'media','model_budget'}
    result=confirm(client,p.json());assert result.status_code==200,result.text
    state=result.json()
    for name in h.NAMES:
        expected=initial if name in ('media','model_budget') else after
        assert state['document']['modules'][name]==expected['document']['modules'][name]
    assert state['versions']['model_budget']>after['versions']['model_budget']
    assert client.get('/api/admin/model-budgets').json()['today_attempts']==1
    assert confirm(client,p.json()).status_code==409
    assert len(rows(client))==2 and rows(client)[0]['source_point']=='before'
    assert client.get(f"{HISTORY}/{record['id']}").json()['after_snapshot']==after


def test_restore_after_snapshot_and_no_change_does_not_write(client):
    apply(client);first=rows(client)[0]
    assert rollback(client,first,point='after').status_code==422
    p=rollback(client,first);assert confirm(client,p.json()).status_code==200
    p=rollback(client,first,point='after');assert p.status_code==200
    assert confirm(client,p.json()).status_code==200
    assert len(rows(client))==3


def test_changed_module_or_late_failure_rolls_back_history_and_all_values(client,monkeypatch):
    from lingxi import model_budgets
    from fastapi import HTTPException
    apply(client);record=rows(client)[0];p=rollback(client,record).json()
    apply(client,['operations'],operations={'log_retention_days':14})
    before=client.get(BASE).json();count=len(rows(client))
    assert confirm(client,p).status_code==409
    p=rollback(client,record).json()
    def failure(*args):raise HTTPException(409,'模拟最后一步失败')
    monkeypatch.setattr(model_budgets,'confirm',failure)
    assert confirm(client,p).status_code==409
    assert client.get(BASE).json()==before and len(rows(client))==count


def test_permissions_authentication_csrf_and_foreign_identity(client):
    apply(client);record=rows(client)[0]
    with TestClient(app) as stranger:
        assert stranger.get(HISTORY).status_code==401
        assert stranger.get(f"{HISTORY}/{record['id']}").status_code==401
    csrf=client.headers.pop('X-CSRF-Token');assert rollback(client,record).status_code==403
    client.headers['X-CSRF-Token']=csrf
    with SessionLocal.begin() as db:db.get(h.ConfigurationRevision,record['id']).workspace_id='foreign'
    assert rows(client)==[]
    assert client.get(f"{HISTORY}/{record['id']}").status_code==404
    assert rollback(client,record).status_code==404


@pytest.mark.parametrize('changes',[{'modules':[]},{'modules':['media','media']},{'modules':['proxy']},
    {'point':'old'},{'versions':{'workspace':True}},{'allow_execution':True}])
def test_invalid_scope_cannot_preview(client,changes):
    apply(client);record=rows(client)[0]
    payload={'point':'before','modules':['workspace'],'versions':{'workspace':1}}|changes
    assert client.post(f"{HISTORY}/{record['id']}/previews",json=payload).status_code==422
    assert len(rows(client))==1


def test_snapshot_change_expiry_and_foreign_preview_cannot_confirm(client):
    apply(client);record=rows(client)[0];p=rollback(client,record).json()
    token=json.loads(decrypt(p['token']));token['expires_at']=now()-1
    assert confirm(client,{'token':encrypt(json.dumps(token))}).status_code==409
    token=json.loads(decrypt(p['token']));token['session']='other'
    assert confirm(client,{'token':encrypt(json.dumps(token))}).status_code==409
    with SessionLocal.begin() as db:
        row=db.get(h.ConfigurationRevision,record['id']);changed=copy.deepcopy(row.before_snapshot)
        changed['document']['modules']['workspace']['workspace_name']='篡改测试快照';row.before_snapshot=changed
    assert confirm(client,p).status_code==409 and len(rows(client))==1


def test_unavailable_persona_is_not_reinstated_or_silently_dropped(client):
    from lingxi.assistants import AssistantProfile
    with SessionLocal() as db:persona=db.scalar(select(AssistantProfile).where(AssistantProfile.kind=='persona')).id
    apply(client,['workspace'],workspace={'default_persona_id':persona})
    record=rows(client)[0]
    apply(client,['workspace'],workspace={'default_persona_id':None,'workspace_name':'另一个空间'})
    with SessionLocal.begin() as db:db.get(AssistantProfile,persona).enabled=False
    assert rollback(client,record,['workspace'],point='after').status_code==409
    assert client.get(BASE).json()['document']['modules']['workspace']['default_persona_id'] is None


def test_history_pages_do_not_skip_or_repeat_and_exports_do_not_leak(configured):
    for n in range(23):apply(configured,['operations'],operations={'log_retention_days':20+n})
    page=configured.get(HISTORY).json();assert len(page['items'])==20 and page['next_before']==4
    tail=configured.get(HISTORY,params={'before':page['next_before']}).json()
    assert [r['number'] for r in page['items']+tail['items']]==list(range(23,0,-1))
    for row in page['items']:
        text=configured.get(f"{HISTORY}/{row['id']}").text
        assert 'secret-fixture' not in text and 'encrypted_key' not in text and 'fixture-model' not in text


def test_latest_history_and_operational_values_survive_older_restore(client):
    from lingxi.backups import checkpoint,quarantine
    from sqlalchemy import text
    apply(client);old=rows(client)[0]
    apply(client,['operations'],operations={'log_retention_days':12,'daily_token_alert':50})
    latest=rows(client)[0]
    with SessionLocal.begin() as db:
        db.execute(text('CREATE TABLE IF NOT EXISTS alembic_version (version_num varchar(32) PRIMARY KEY)'))
        db.execute(text('DELETE FROM alembic_version'));db.execute(text("INSERT INTO alembic_version VALUES ('0060')"))
    saved=checkpoint()
    with SessionLocal.begin() as db:
        db.execute(delete(h.ConfigurationRevision).where(h.ConfigurationRevision.id==latest['id']))
        pref=db.scalar(select(OpsPreference));pref.log_retention_days=90;pref.daily_token_alert=0
    quarantine(saved)
    with SessionLocal() as db:
        pref=db.scalar(select(OpsPreference));assert pref.log_retention_days==12 and pref.daily_token_alert==50
        records=list(db.scalars(select(h.ConfigurationRevision).order_by(h.ConfigurationRevision.number)))
        assert [r.id for r in records[:2]]==[old['id'],latest['id']]
        assert records[-1].action=='restore' and records[-1].number==3
    assert client.get(BASE).status_code==401


@pytest.mark.parametrize('case',['foreign','source','state','domain','operations'])
def test_invalid_checkpoint_rejected_before_current_configuration_changes(client,case):
    from lingxi.backups import checkpoint,quarantine
    from sqlalchemy import text
    apply(client)
    with SessionLocal.begin() as db:
        db.execute(text('CREATE TABLE IF NOT EXISTS alembic_version (version_num varchar(32) PRIMARY KEY)'))
        db.execute(text('DELETE FROM alembic_version'));db.execute(text("INSERT INTO alembic_version VALUES ('0060')"))
    saved=checkpoint();part=saved['configuration_history']
    if case=='foreign':part['revisions'][0]['user_id']='another'
    elif case=='source':part['revisions'][0].update(action='rollback',source_id='missing',source_point='before')
    elif case=='state':part['state']['versions'].pop('media')
    elif case=='domain':part['state']['document']['modules']['workspace']['workspace_name']='不匹配'
    else:part['operations'][0]['daily_token_alert']=999
    with pytest.raises(ValueError):quarantine(saved)
    assert client.get(BASE).status_code==200 and len(rows(client))==1
