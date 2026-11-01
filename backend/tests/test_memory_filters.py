"""Explicit subject/time lookup must not invent dates or freeze deleted facts."""
from datetime import datetime
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from lingxi.api import app
from lingxi.db import SessionLocal, engine, now
from lingxi.models import Memory


def stamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def add(*rows):
    with SessionLocal.begin() as db:
        for identity, fields in rows:
            db.add(Memory(**({'id': identity, 'user_id': 'owner', 'content': '共同项目',
                'category': 'projects', 'created_at': now()-100} | fields)))


def read(client, **params):
    r=client.get('/api/memory-pages', params=params)
    assert r.status_code==200, r.text
    return r.json()


def ids(data):
    return {row['id'] for row in data['items']}


def test_subject_is_exact_and_combines_with_literal_text_category_and_scope(client):
    add(('self', {}), ('friend', {'subject':'朋友_100%'}), ('prefix', {'subject':'朋友_100%小明'}),
        ('wrong-category', {'subject':'朋友_100%', 'category':'preferences'}),
        ('foreign', {'subject':'朋友_100%', 'workspace_id':'other'}),
        ('stranger', {'subject':'朋友_100%', 'user_id':'stranger'}))
    assert ids(read(client, subject='self'))=={'self'}
    assert ids(read(client, subject='朋友_100%', category='projects', q='项目'))=={'friend'}
    assert not read(client, subject='朋友')['items']
    with TestClient(app) as anonymous:
        assert anonymous.get('/api/memory-pages?subject=self&time_basis=event').status_code==401


def test_event_dates_are_utc_days_and_do_not_use_recorded_date(client):
    add(('before', {'event_at':'2026-10-31T23:59:59Z'}),
        ('begin', {'event_at':'2026-11-01'}), ('late', {'event_at':'2026-11-01T23:59:59.999999Z'}),
        ('offset-out', {'event_at':'2026-11-01T00:00:00+08:00'}),
        ('offset-in', {'event_at':'2026-11-02T07:59:59+08:00'}),
        ('after', {'event_at':'2026-11-02'}), ('unknown', {}),
        ('legacy', {'event_at':'去年春天'}), ('invalid-date', {'event_at':'2026-02-30'}),
        ('naive', {'event_at':'2026-11-01T08:00:00'}))
    params={'time_basis':'event','since':'2026-11-01','until':'2026-11-01'}
    assert ids(read(client, **params))=={'begin','late','offset-in'}
    assert len(read(client)['items'])==10
    assert ids(read(client, time_basis='event', until='2026-10-31'))=={'before','offset-out'}
    assert ids(read(client, time_basis='event', since='2026-11-02'))=={'after'}


def test_period_overlap_requires_known_ordered_endpoints_not_infinite_unknowns(client):
    add(('overlap', {'valid_from':'2026-10-01','valid_to':'2026-12-31'}),
        ('ends-on-date', {'valid_from':'2026-10-01','valid_to':'2026-11-01'}),
        ('starts-on-date', {'valid_from':'2026-11-01','valid_to':'2026-11-01'}),
        ('before', {'valid_from':'2026-10-01','valid_to':'2026-10-31'}),
        ('after', {'valid_from':'2026-11-02','valid_to':'2026-11-03'}),
        ('no-start', {'valid_to':'2026-12-31'}), ('no-end', {'valid_from':'2026-10-01'}),
        ('legacy', {'valid_from':'去年','valid_to':'明年'}),
        ('reversed', {'valid_from':'2026-12-31','valid_to':'2026-10-01'}), ('unknown', {}))
    assert ids(read(client, time_basis='period', since='2026-11-01', until='2026-11-01'))=={
        'overlap','ends-on-date','starts-on-date'}
    assert ids(read(client, time_basis='period'))=={'overlap','ends-on-date','starts-on-date','before','after'}


def test_record_date_is_independent_of_event_time_and_is_inclusive(client):
    add(('first', {'created_at':stamp('2026-09-01T00:00:00Z'),'event_at':'2025-01-01'}),
        ('last', {'created_at':stamp('2026-09-01T23:59:59Z')}),
        ('older', {'created_at':stamp('2026-08-31T23:59:59Z')}),
        ('newer', {'created_at':stamp('2026-09-02T00:00:00Z')}))
    assert ids(read(client, time_basis='created', since='2026-09-01', until='2026-09-01'))=={'first','last'}


@pytest.mark.parametrize('params', [
    {'time_basis':'guessed'}, {'subject':'x'*151}, {'since':'2026-11-01'},
    {'time_basis':'event','since':'2026-02-30'}, {'time_basis':'period','until':'0000-01-01'},
    {'time_basis':'created','since':'2026-11-02','until':'2026-11-01'},
    {'time_basis':'event','since':'2026-1-1'}, {'time_basis':'event','until':'昨天'},
    {'time_basis':'event','since':'2026-11-01T00:00:00Z'}])
def test_invalid_filters_fail_explicitly(client, params):
    assert client.get('/api/memory-pages',params=params).status_code==422


def test_filter_changes_cannot_reuse_a_cursor_and_no_scope_is_encoded_by_client(client):
    add(*[(str(i), {'event_at':'2026-11-01','subject':'self'}) for i in range(3)])
    params={'subject':'self','time_basis':'event','since':'2026-11-01','until':'2026-11-02','limit':1}
    cursor=read(client, **params)['next_cursor']
    assert cursor
    for change in [{'subject':'friend'}, {'time_basis':'period'}, {'since':'2026-10-31'}, {'until':'2026-11-03'}]:
        assert client.get('/api/memory-pages',params=params|change|{'cursor':cursor}).status_code==400


def test_sparse_temporal_pages_are_bounded_and_do_not_claim_empty_bank(client):
    timestamp=now()-100
    add(*[(f'row-{i:04}', {'created_at':timestamp,'event_at':'2026-11-01' if i<2 else None}) for i in range(1100)])
    queries=[]
    def count(conn,cursor,statement,parameters,context,executemany):
        if 'FROM memories' in statement:
            queries.append(statement)
    event.listen(engine,'before_cursor_execute',count)
    try:
        first=read(client, time_basis='event')
    finally:
        event.remove(engine,'before_cursor_execute',count)
    assert first['items']==[] and first['next_cursor']
    assert first['scan_limited'] and first['scanned']==1024
    assert len(queries)<=17 and all('LIMIT' in statement for statement in queries)
    second=read(client, time_basis='event',cursor=first['next_cursor'])
    assert ids(second)=={'row-0000','row-0001'}
    assert second['scanned']==76 and not second['scan_limited'] and not second['next_cursor']


def test_full_temporal_pages_do_not_skip_unexamined_rows_and_deletion_is_live(client):
    add(*[(f'row-{i:03}', {'event_at':'2026-11-01'}) for i in range(70)])
    first=read(client, time_basis='event', limit=2)
    assert ids(first)=={'row-069','row-068'}
    with SessionLocal.begin() as db:
        db.get(Memory,'row-068').status='deleted'  # Removing cursor anchor is safe.
        db.get(Memory,'row-067').event_at='2020-01-01'
        db.get(Memory,'row-066').status='deleted'
    seen=set(ids(first));cursor=first['next_cursor']
    while cursor:
        # The original filter deliberately has no lower bound. A date correction
        # remains visible under that filter, while deletion is always current.
        data=read(client, time_basis='event',cursor=cursor,limit=2)
        assert not (seen & ids(data))
        seen |= ids(data);cursor=data['next_cursor']
    assert len(seen)==69 and 'row-066' not in seen


def test_time_correction_and_delete_are_rechecked_between_sparse_pages(client):
    add(*[(f'row-{i:04}', {'event_at':None}) for i in range(1030)])
    first=read(client, time_basis='event',since='2026-11-01')
    with SessionLocal.begin() as db:
        db.get(Memory,'row-0000').event_at='2026-11-01'
        db.get(Memory,'row-0001').event_at='2020-01-01'
        db.get(Memory,'row-0002').event_at='2026-11-01'
        db.get(Memory,'row-0002').status='deleted'
    second=read(client, time_basis='event',since='2026-11-01',cursor=first['next_cursor'])
    assert ids(second)=={'row-0000'}
