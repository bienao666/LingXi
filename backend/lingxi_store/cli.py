import argparse
import base64
import getpass
import json
from pathlib import Path
import re
import secrets
import uuid
from argon2 import PasswordHasher
from sqlalchemy import select
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from lingxi.plugin_signing import canonical, key_id, load_private, public_key, sha, sign
from .data import Account, Login, Reader, State, now, session, settings


def write_secret(path, data):
    with path.open('x', encoding='utf-8') as file:
        file.write(data)
    path.chmod(0o600)


def keys(directory, root_path=None, revoke=None):
    directory.mkdir(parents=True, exist_ok=True)
    registry = directory/'trust.json'
    if root_path:
        root = load_private(root_path)
        old = json.loads(registry.read_text())
        from lingxi.plugin_signing import verify
        verify(public_key(root), old['payload'], old['signature'])
        records, epoch = old['payload']['keys'], old['payload']['epoch']+1
        if revoke and revoke not in {k['key_id'] for k in records}:
            raise SystemExit('Unknown signing key')
        records = [dict(k, status='revoked') if k['key_id'] == revoke else k for k in records]
    else:
        if registry.exists() or (directory/'signing.key').exists():
            raise SystemExit('Existing keys are never overwritten by initialization')
        root, records, epoch = Ed25519PrivateKey.generate(), [], 1
        write_secret(directory/'offline-root.key', base64.b64encode(root.private_bytes_raw()).decode())
    private = Ed25519PrivateKey.generate()
    pub = public_key(private)
    records.append({'key_id':key_id(pub), 'public_key':pub, 'status':'active'})
    payload = {'format':'lingxi-trust-v1', 'epoch':epoch, 'keys':records}
    data = {'root_public_key':public_key(root), 'payload':payload, 'signature':sign(root,payload)}
    # Stop the store during operator rotation; atomic file replacements individually.
    temp = directory/('signing.'+uuid.uuid4().hex+'.tmp')
    write_secret(temp, base64.b64encode(private.private_bytes_raw()).decode())
    temp.replace(directory/'signing.key')
    temp = directory/('trust.'+uuid.uuid4().hex+'.tmp')
    temp.write_bytes(canonical(data))
    temp.replace(registry)
    print('Pinned root public key (public, copy via a trusted channel):',public_key(root))
    print('Active online signing key:',key_id(pub),'trust epoch:',epoch)


def main():
    p = argparse.ArgumentParser(description='Independent store operator commands; no assistant credentials')
    sub = p.add_subparsers(dest='command', required=True)
    for name in ('init-keys','rotate-key'):
        k = sub.add_parser(name)
        k.add_argument('--directory',type=Path,required=True)
        if name == 'rotate-key':
            k.add_argument('--root-key',type=Path,required=True)
            k.add_argument('--revoke')
    u = sub.add_parser('create-account')
    u.add_argument('--username', required=True)
    u.add_argument('--role', choices=['publisher','reviewer'], required=True)
    u.add_argument('--namespace')
    u.add_argument('--audience', action='append', default=[])
    r = sub.add_parser('issue-reader')
    r.add_argument('--audience', required=True)
    r.add_argument('--token-file', type=Path, required=True)
    r.add_argument('--days', type=int,default=90)
    r = sub.add_parser('revoke-reader');r.add_argument('--id',required=True)
    r = sub.add_parser('disable-account');r.add_argument('--username',required=True)
    args = p.parse_args()
    if args.command in ('init-keys','rotate-key'):
        keys(args.directory, getattr(args,'root_key',None),getattr(args,'revoke',None));return
    with session() as db, db.begin():
        if args.command == 'create-account':
            namespace = args.namespace if args.role == 'publisher' else None
            if namespace == 'lingxi' or (args.role == 'publisher' and (not namespace or not re.fullmatch('[a-z][a-z0-9-]{0,59}',namespace))):
                raise SystemExit('Choose a nonreserved publisher namespace')
            name=args.username.strip().casefold()
            if not name or len(name)>100 or db.scalar(select(Account).where(Account.username==name)):
                raise SystemExit('Invalid or existing account')
            password=getpass.getpass('Store password (12+ characters): ')
            if not 12<=len(password)<=300 or password!=getpass.getpass('Confirm password: '):
                raise SystemExit('Invalid password or confirmation')
            if any(not a or len(a)>100 for a in args.audience):
                raise SystemExit('Invalid private audience')
            db.add(Account(username=name,role=args.role,namespace=namespace,audiences=list(dict.fromkeys(args.audience)),password_hash=PasswordHasher().hash(password)))
            print('Independent store account created.')
        elif args.command == 'issue-reader':
            if not 1<=args.days<=365 or not args.audience or len(args.audience)>100:
                raise SystemExit('Invalid audience or lifetime')
            token=secrets.token_urlsafe(48);identity=uuid.uuid4().hex
            write_secret(args.token_file,token)
            db.add(Reader(id=identity,token_hash=sha(token.encode()),audience=args.audience,expires_at=now()+args.days*86400))
            print('Read-only token written to the named private file; ID:',identity)
        elif args.command == 'revoke-reader':
            row=db.get(Reader,args.id)
            if not row:raise SystemExit('Unknown reader')
            row.enabled=False;print('Reader revoked.')
        else:
            row=db.scalar(select(Account).where(Account.username==args.username.casefold()))
            if not row:raise SystemExit('Unknown account')
            row.enabled=False
            for login in db.scalars(select(Login).where(Login.account_id==row.id)):db.delete(login)
            print('Account disabled and login sessions revoked.')


if __name__=='__main__':main()
