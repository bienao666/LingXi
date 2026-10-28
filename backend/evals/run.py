"""Real-service evaluation runner. Only runs against an empty, dedicated evaluation owner."""
import argparse
import getpass
import json
import time
from pathlib import Path
import httpx
from dataset import build


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://localhost:8080')
    parser.add_argument('--output', default='backend/evals/results.json')
    parser.add_argument('--limit', type=int, default=150)
    args=parser.parse_args()
    client=httpx.Client(base_url=args.base_url,timeout=30)
    def api(method,path,**kwargs):
        r=client.request(method,path,**kwargs);r.raise_for_status();return r.json()
    auth=api('POST','/api/auth/login',json={'username':'eval-lingxi','password':getpass.getpass('Dedicated eval-lingxi password: ')})
    client.headers['X-CSRF-Token']=auth['csrf']
    if api('GET','/api/conversations') or api('GET','/api/memories'):
        raise SystemExit('Evaluation requires an empty isolated deployment. No user data was changed.')
    def wait_run(run_id):
        for _ in range(180):
            run=api('GET','/api/chat-runs/'+run_id)
            if run['status'] not in ('queued','running'):
                if run['status']!='completed' or run['memory_status']!='ready':
                    raise RuntimeError('Real model or Hindsight is not ready: '+str(run['status']))
                return run
            time.sleep(1)
        raise TimeoutError('Chat did not finish')
    results=[]
    for case in build()[:args.limit]:
        outcome={'id':case['id'],'group':case['group'],'status':'not_run'}
        created=[];conversations=[]
        try:
            # Retrieval/lifecycle benchmark: fixtures are explicit user-confirmed memory entries.
            # Extraction is tested separately; do not report this as extraction accuracy.
            for fact in case['facts']:
                m=api('POST','/api/memories',json={'content':fact,'category':'experiences'})
                created.append(m)
            for m in created:
                for _ in range(120):
                    current=api('GET','/api/memories/'+m['id'])
                    if current['index_status']=='ready':break
                    if current['index_status']=='failed':raise RuntimeError('Indexing failed')
                    time.sleep(1)
                else:raise TimeoutError('Index not ready')
            action=case['action'] or {}
            if action.get('kind')=='correct':
                m=api('PATCH','/api/memories/'+created[0]['id'],json={'content':action['content'],'version':1,'category':'experiences'})
                for _ in range(120):
                    if api('GET','/api/memories/'+m['id'])['index_status']=='ready':break
                    time.sleep(1)
                else:raise TimeoutError('Corrected index not ready')
            if action.get('kind')=='delete':
                for m in created:api('DELETE','/api/memories/'+m['id'])
            if action.get('kind')=='restart':
                input(f"{case['id']}: restart Core/worker/Hindsight now, then press Enter: ")
            c=api('POST','/api/conversations',json={'title':'EVAL '+case['id']});conversations.append(c['id'])
            body={'conversation_id':c['id'],'request_id':'eval-'+case['id'],'content':case['query']}
            run=api('POST','/api/chat',json=body)
            if action.get('kind')=='redelivery':
                assert api('POST','/api/chat',json=body)['id']==run['id']
            wait_run(run['id'])
            messages=api('GET','/api/messages',params={'conversation_id':c['id']})
            answer=next(m['content'] for m in reversed(messages) if m['role']=='assistant')
            passed=all(s in answer for s in case['expected']) and not any(s in answer for s in case['forbidden'])
            manual=action.get('kind') in ('uncertain','delete')
            outcome.update(status='needs_review' if manual else ('passed' if passed else 'failed'),answer=answer,
                           automated_assertions_passed=passed)
        except Exception as exc:
            outcome.update(status='error',error=type(exc).__name__)
        finally:
            for m in created:
                client.delete('/api/memories/'+m['id'])
            for c in conversations:
                client.delete('/api/conversations/'+c,params={'delete_memories':'true'})
        results.append(outcome)
        Path(args.output).write_text(json.dumps({'scope':'retrieval and lifecycle on user-confirmed fixtures; not extraction accuracy',
            'status':'pending_manual_review','results':results},ensure_ascii=False,indent=2),encoding='utf-8')
        print(outcome['id'],outcome['status'])


if __name__=='__main__':main()
