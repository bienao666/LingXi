import argparse
import getpass
import json
import os
import secrets
from pathlib import Path
from cryptography.fernet import Fernet
from sqlalchemy import select


def configure():
    target = Path('.env')
    if target.exists():
        raise SystemExit('.env already exists; edit it in place. No existing secrets overwritten.')
    template = Path('.env.example').read_text(encoding='utf-8')
    replacements = {'CHANGE_ENCRYPTION_KEY': Fernet.generate_key().decode(),
                    'CHANGE_DB_PASSWORD': secrets.token_hex(24),
                    'CHANGE_HINDSIGHT_DB_PASSWORD': secrets.token_hex(24)}
    replacements['CHANGE_MEMORY_GATEWAY_TOKEN'] = secrets.token_urlsafe(48)
    for key, value in replacements.items():
        template = template.replace(key, value)
    target.write_text(template, encoding='utf-8')
    try:
        target.chmod(0o600)
    except OSError:
        pass
    print('Created .env with independent random encryption/database keys. No model key configured.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['configure', 'init-admin', 'reset-totp', 'replay-deletions', 'openapi', 'reindex'])
    args = parser.parse_args()
    if args.command == 'configure':
        configure()
        return
    from .db import SessionLocal
    if args.command == 'openapi':
        from .api import app
        Path('backend/openapi.json').write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=2), encoding='utf-8')
        print('Updated backend/openapi.json')
        return
    from .models import User
    from .security import hasher
    with SessionLocal.begin() as db:
        if args.command == 'init-admin':
            if db.scalar(select(User)):
                raise SystemExit('An owner already exists. Initialization cannot overwrite it.')
            username = input('Owner username: ').strip()
            password = getpass.getpass('Owner password (12+ characters): ')
            if not username or len(username) > 100 or len(password) < 12 or len(password) > 300:
                raise SystemExit('Invalid username or password length')
            if password != getpass.getpass('Confirm password: '):
                raise SystemExit('Passwords differ')
            db.add(User(username=username, password_hash=hasher.hash(password)))
            db.flush()
            from .assistants import seed_defaults
            seed_defaults(db)
            from .media_policy import seed_defaults as seed_media_policy
            seed_media_policy(db)
            print('Owner initialized.')
        elif args.command == 'reset-totp':
            from .auth_totp import clear_factor, configuration, lock_owner
            from .models import LoginSession
            from .web import audit
            from sqlalchemy import delete
            from argon2.exceptions import VerifyMismatchError, InvalidHashError
            username = input('Owner username: ').strip()
            user = db.scalar(select(User).where(User.username == username))
            if not user:
                raise SystemExit('Owner not found.')
            user = lock_owner(db, user)
            try:
                hasher.verify(user.password_hash, getpass.getpass('Current owner password: '))
            except (VerifyMismatchError, InvalidHashError):
                raise SystemExit('Password verification failed; no changes made.') from None
            if input('This disables TOTP and revokes all sessions. Type RESET '+username+': ') != 'RESET '+username:
                raise SystemExit('Cancelled; no changes made.')
            row = configuration(db, user)
            if row:
                clear_factor(row)
            db.execute(delete(LoginSession).where(LoginSession.user_id == user.id))
            audit(db, user, 'totp_operator_reset')
            print('TOTP reset; all browser sessions revoked. Sign in and enroll a new authenticator.')
        elif args.command == 'reindex':
            from .models import Job, Memory, Model, Policy
            from .db import now
            from .memory import enqueue
            from .gateway import resolve_model
            if db.scalar(select(Job).where(Job.status=='running', Job.lease_until > now())):
                raise SystemExit('Stop Core and workers and wait for all leases to expire before reindexing.')
            model_id = input('Verified embedding model ID: ').strip()
            model, _, _ = resolve_model(db, 'embedding', model_id)
            if model.purpose != 'embedding':
                raise SystemExit('Wrong model purpose')
            policy = db.get(Policy, 'embedding')
            if not policy:
                raise SystemExit('Configure the initial embedding policy first.')
            policy.model_id, policy.version = model.id, policy.version+1
            for memory in db.scalars(select(Memory).where(Memory.status=='active')):
                memory.index_status = 'pending'
                enqueue(db, f'reindex:{policy.version}:{memory.id}:{memory.version}', 'index', {'memory_id':memory.id,'version':memory.version})
            print('Queued a new isolated index generation. Start Hindsight/Core/workers to rebuild; no old vectors are reused.')
        else:
            from .memory import replay_ledger
            print('Replayed deletion ledger:', replay_ledger(db))


if __name__ == '__main__':
    main()
