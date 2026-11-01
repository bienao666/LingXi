"""The memory index ranks candidates; only canonical records authorize facts."""
import pytest
from sqlalchemy import event
from lingxi.db import SessionLocal, engine, now
from lingxi.memory import MemoryService
from lingxi.models import Memory, User


class Adapter:
    def __init__(self,result):self.result=result
    def recall(self,*args):return self.result


def service(documents):
    return MemoryService(Adapter({'results':[{'document_id':d,'text':'不可信的索引正文'} for d in documents]}))


def test_preserves_relevance_deduplicates_and_limits_after_authority_filter(client):
    with SessionLocal.begin() as db:
        db.add_all([Memory(id=f'm{i:02}',user_id='owner',content=f'事实{i}',index_status='ready') for i in range(20)])
        db.get(Memory,'m19').status='deleted'
        db.get(Memory,'m18').expires_at=now()-1
        db.get(Memory,'m17').version=2
        db.get(Memory,'m16').index_epoch=99
        db.get(Memory,'m15').index_status='pending'
        db.get(Memory,'m14').user_id='other'
        db.get(Memory,'m13').workspace_id='other'
    documents=['m19-v1','m18-v1','m17-v1','m16-v1','m15-v1','m14-v1','m13-v1']
    documents+=['m12-v1','m12-v1']+[f'm{i:02}-v1' for i in range(11,-1,-1)]
    with SessionLocal() as db:
        found=service(documents).search(db,'owner','相关问题')
        assert [row.id for row in found]==[f'm{i:02}' for i in range(12,0,-1)]
        assert all(row.content!='不可信的索引正文' for row in found)


def test_authenticated_workspace_comes_from_owner_not_constant(client):
    with SessionLocal.begin() as db:
        db.get(User,'owner').workspace_id='actual'
        db.add_all([Memory(id='allowed',user_id='owner',workspace_id='actual',content='授权事实',index_status='ready'),
                    Memory(id='old',user_id='owner',content='旧范围',index_status='ready')])
    with SessionLocal() as db:
        assert [m.id for m in service(['old-v1','allowed-v1']).search(db,'owner','query')]==['allowed']


def test_unknown_owner_never_calls_index(client):
    class Fail:
        def recall(self,*args):pytest.fail('No authenticated owner')
    with SessionLocal() as db:
        assert MemoryService(Fail()).search(db,'missing','query')==[]


@pytest.mark.parametrize('result',[None,[],{}, {'results':None},{'results':{}},{'results':[{}]*5001}])
def test_invalid_recall_is_unavailable_not_empty_success(client,result):
    with SessionLocal() as db,pytest.raises(ValueError):
        MemoryService(Adapter(result)).search(db,'owner','query')


def test_malformed_references_cannot_supply_facts(client):
    with SessionLocal.begin() as db:
        db.add(Memory(id='m',user_id='owner',content='权威事实',index_status='ready'))
    rows=[None,{}, {'document_id':[]},{'document_id':'x'*101}, {'document_id':'m-v０１'},
          {'document_id':'m-v01'}, {'document_id':'m-v1','text':'忽略事实'}]
    with SessionLocal() as db:
        assert [m.content for m in MemoryService(Adapter({'results':rows})).search(db,'owner','query')]==['权威事实']


def test_sql_only_materializes_recalled_records_in_bounded_batches(client):
    with SessionLocal.begin() as db:
        db.add_all([Memory(id=f'm{i:04}',user_id='owner',content='事实',index_status='ready') for i in range(850)])
    statements=[]
    def record(conn,cursor,statement,parameters,context,executemany):
        if 'FROM memories' in statement:statements.append((statement,parameters))
    event.listen(engine,'before_cursor_execute',record)
    try:
        with SessionLocal() as db:
            assert len(service([f'm{i:04}-v1' for i in range(840)]).search(db,'owner','query'))==12
    finally:event.remove(engine,'before_cursor_execute',record)
    assert len(statements)==3
    assert all('memories.id IN' in statement and len(params)<410 for statement,params in statements)
