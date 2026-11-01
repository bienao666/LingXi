#!/usr/bin/env python3
"""Linux server-to-server handover. Never accepts SSH credentials or remote URLs.

Operators transfer the protected archive directory through their trusted channel.
The same backup runner lock prevents concurrent backup/restore/migration work.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backup import Runner, BackupError, atomic_json, digest, project_exclusive, identity, verify


class MigrationRunner(Runner):
    @property
    def journal(self):
        return self.directory / 'migration-in-progress.json'

    def migration(self, action, data, offline=False):
        return self.core('migration:' + action, data, offline=offline)

    def guard(self):
        if (self.directory / 'in-progress.json').exists():
            raise BackupError('Recover the interrupted backup operation first')

    def prepare(self, label):
        self.guard()
        if self.journal.exists():
            raise BackupError('Recover the interrupted migration first')
        result = self.migration('prepare-target', {'label': label})
        path = self.directory / 'target-registration.json'
        atomic_json(path, result)
        print('Target prepared; business writes and worker scheduling are paused.', flush=True)
        print('Registration saved in target-registration.json; copy its ticket into the source migration preview.', flush=True)

    def wait_idle(self):
        for _ in range(60):
            if self.core('idle', offline=True)['idle']:
                return
            time.sleep(1)
        raise BackupError('Source remains frozen while active work drains; retry export after it finishes')

    def current_services(self, recorded):
        current = self.containers()
        if set(current) != set(recorded) or any(
            current[key]['id'] != value['id'] or current[key]['image'] != value['image']
            or current[key].get('volumes') != value.get('volumes') for key, value in recorded.items()):
            raise BackupError('Recorded containers or images changed; retain the fence and inspect manually')
        return current

    def status_services(self, services):
        # Keep only status/login available. Never restart the old QQ process,
        # worker, subscriptions or Hindsight while a source/target is fenced.
        self.resume({key: {**value, 'running': value['running'] and key in ('core', 'web')}
            for key, value in services.items()})

    def export(self, migration_id):
        self.guard()
        identity(migration_id)
        if self.journal.exists():
            progress = json.loads(self.journal.read_text())
            if progress.get('phase') != 'export' or progress.get('id') != migration_id:
                raise BackupError('A different migration requires recovery')
            services = progress['services']
            self.offline_image = services['core']['image']
        else:
            services = self.containers()
            if input('Source writes will remain frozen after export. Type EXPORT ' + migration_id + ': ') != 'EXPORT ' + migration_id:
                raise BackupError('Cancelled')
            atomic_json(self.journal, {'phase': 'export', 'id': migration_id, 'services': services})
        try:
            current = self.current_services(services)
            self.migration('freeze', {'id': migration_id}, offline=True)
            self.wait_idle()
            self.stop(current)
            archive = self.directory / migration_id
            if not archive.exists():
                partial = self.directory / (migration_id + '.partial')
                if partial.exists():
                    os.rename(partial, self.directory / (migration_id + '.interrupted-' + uuid.uuid4().hex))
                self.snapshot({'id': migration_id, 'archive_id': migration_id}, services)
            manifest = verify(archive)
            checkpoint = json.loads((self.directory / 'latest-safety.json').read_text())
            result = self.migration('seal', {'id': migration_id, 'manifest_digest': digest(archive / 'manifest.json'),
                'checkpoint': checkpoint}, offline=True)
            atomic_json(archive / 'handover.json', {'handover': result['handover']})
            self.status_services(services)
            self.journal.unlink()
            print('Handover archive verified:', migration_id, flush=True)
            print('Source remains frozen. Transfer this entire archive directory; keep the master key separate.', flush=True)
        except Exception:
            # No automatic release, retry, external replay or guessed completion.
            raise BackupError('Export interrupted. Source may be frozen; retain files and rerun export with the same ID. Do not resume the old deployment for work.') from None

    def bundle(self, migration_id):
        archive = self.directory / identity(migration_id)
        manifest = verify(archive)
        path = archive / 'handover.json'
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 256 * 1024**2:
            raise BackupError('Handover proof missing or too large')
        data = json.loads(path.read_text())
        if set(data) != {'handover'} or not isinstance(data['handover'], str):
            raise BackupError('Invalid handover proof')
        return archive, manifest, {'handover': data['handover'], 'manifest_digest': digest(archive / 'manifest.json')}

    def inspect(self, migration_id):
        self.guard()
        _, _, data = self.bundle(migration_id)
        result = self.migration('inspect', data)
        if result['id'] != migration_id:
            raise BackupError('Handover ID mismatch')
        print('Archive, exact target registration, origin, schema and encrypted handover verified.', flush=True)
        print('Source authority record counts:', json.dumps(result['counts'], sort_keys=True), flush=True)
        return result

    def import_archive(self, migration_id):
        self.guard()
        if self.journal.exists():
            raise BackupError('Recover the interrupted target import first')
        archive, manifest, payload = self.bundle(migration_id)
        result = self.migration('inspect', payload)
        if result['id'] != migration_id:
            raise BackupError('Handover ID mismatch')
        services = self.containers()
        if any(manifest.get('images', {}).get(name) != services[name]['image']
                for name in ('core', 'worker', 'db', 'hindsight')):
            raise BackupError('Target Core, worker, database and Hindsight must use the exact source images')
        self.preflight_restore(services, manifest)
        if input('Initial target administrator/defaults will be replaced. Type IMPORT ' + migration_id + ': ') != 'IMPORT ' + migration_id:
            raise BackupError('Cancelled')
        if not self.core('idle')['idle']:
            raise BackupError('Target has active work')
        rollback_id = uuid.uuid4().hex
        record = {'phase': 'import_prepare', 'id': migration_id, 'services': services, 'rollback_id': rollback_id}
        atomic_json(self.journal, record)
        try:
            self.stop(services)
            self.snapshot({'id': rollback_id, 'archive_id': rollback_id}, services)
            record['phase'] = 'import'
            atomic_json(self.journal, record)
            self.apply_archive(archive, manifest, services, payload | {'target': result['target']},
                quarantine_action='migration:imported', replace_files=True)
            self.status_services(services)
            self.journal.unlink()
        except Exception:
            raise BackupError('Target import interrupted; services remain stopped. Run recover before retrying; the source remains frozen.') from None
        print('Import verified; target is awaiting activation. Old sessions, external grants and pending work are quarantined.', flush=True)
        print('Rollback archive:', rollback_id, flush=True)

    def activate(self, migration_id):
        self.guard()
        if self.journal.exists():
            raise BackupError('Recover the interrupted migration before activation')
        identity(migration_id)
        if input('Keep source frozen; reauthorize external services after activation. Type ACTIVATE ' + migration_id + ': ') != 'ACTIVATE ' + migration_id:
            raise BackupError('Cancelled')
        self.migration('activate', {'id': migration_id})
        print('Target activated. Reauthorize models, plugins, resource scopes, contact preferences and QQ explicitly.', flush=True)
        print('Start target worker and Hindsight with Docker Compose after reviewing configuration. NapCat remains stopped.', flush=True)

    def recover_migration(self):
        self.guard()
        if not self.journal.exists():
            raise BackupError('No interrupted target migration')
        data = json.loads(self.journal.read_text())
        if data.get('phase') == 'export':
            raise BackupError('Retry export with the recorded ID; source writes must remain frozen')
        services = data['services']
        self.offline_image = services['core']['image']
        self.stop(self.current_services(services))
        if data['phase'] == 'import':
            archive = self.directory / identity(data['rollback_id'])
            manifest = verify(archive)
            self.preflight_restore(services, manifest)
            if input('Restore the empty prepared target. Type ROLLBACK ' + data['rollback_id'] + ': ') != 'ROLLBACK ' + data['rollback_id']:
                raise BackupError('Cancelled')
            safety = json.loads((self.directory / 'latest-safety.json').read_text())
            self.apply_archive(archive, manifest, services, safety, quarantine_action='migration:recover-target', replace_files=True)
        elif data['phase'] != 'import_prepare':
            raise BackupError('Unknown target migration phase')
        self.status_services(services)
        self.journal.unlink()
        print('Prepared target recovered; source still frozen. Verify and retry the same handover archive.', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare-target', 'export', 'inspect', 'import', 'activate', 'recover'])
    parser.add_argument('migration_id', nargs='?')
    parser.add_argument('--label', default='新服务器')
    parser.add_argument('--project-dir', default='.')
    parser.add_argument('--project-name', default='lingxi')
    parser.add_argument('--migration-dir', required=True)
    args = parser.parse_args()
    try:
        runner = MigrationRunner(args.project_dir, args.migration_dir, args.project_name)
        with project_exclusive(runner):
            if args.action == 'prepare-target': runner.prepare(args.label)
            elif args.action == 'export': runner.export(args.migration_id or '')
            elif args.action == 'inspect': runner.inspect(args.migration_id or '')
            elif args.action == 'import': runner.import_archive(args.migration_id or '')
            elif args.action == 'activate': runner.activate(args.migration_id or '')
            else: runner.recover_migration()
    except (BackupError, ValueError, KeyError, TypeError, OSError, EOFError):
        raise SystemExit('Migration did not complete. Inspect protected runner logs and the recorded phase; do not release source writes or repeat external actions.') from None


if __name__ == '__main__': main()
