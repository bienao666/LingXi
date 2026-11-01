"""Exercise the packaged Alembic graph rather than stamping the code constant."""
import os
from pathlib import Path
import subprocess
import sys
import uuid

from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema, DropSchema

from lingxi import workspace_migrations as m


BACKEND = Path(__file__).resolve().parents[1]


def test_packaged_head_does_not_depend_on_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    actual_head = ScriptDirectory(str(BACKEND / 'migrations')).get_current_head()
    assert m.migration_schema() == actual_head == m.SCHEMA


def test_real_postgresql_upgrade_matches_handover_schema(tmp_path):
    value = os.environ.get('LINGXI_TEST_DATABASE_URL', '')
    if not value.startswith('postgresql'):
        pytest.skip('Full migrations require the isolated PostgreSQL test database')
    # Use a fresh namespace; never upgrade or stamp the shared test tables.
    namespace = 'handover_upgrade_' + uuid.uuid4().hex
    admin = create_engine(value)
    with admin.begin() as db:
        db.execute(CreateSchema(namespace))
    url = make_url(value).update_query_dict({'options': '-csearch_path=' + namespace})
    env = dict(os.environ, LINGXI_DATABASE_URL=url.render_as_string(hide_password=False),
               PYTHONPATH=str(BACKEND))
    # Absolute paths deliberately avoid relying on the server's /app cwd.
    script = ('from alembic.config import Config; from alembic import command; '
              'import sys; config=Config(sys.argv[1]); '
              'config.set_main_option("script_location", sys.argv[2]); '
              'command.upgrade(config, "head")')
    engine = create_engine(url)
    try:
        result = subprocess.run([sys.executable, '-c', script, str(BACKEND / 'alembic.ini'),
                                 str(BACKEND / 'migrations')], cwd=tmp_path, env=env,
                                capture_output=True, text=True, timeout=90)
        assert result.returncode == 0, result.stderr
        actual_head = ScriptDirectory(str(BACKEND / 'migrations')).get_current_head()
        with Session(engine) as db:
            assert list(db.scalars(text('SELECT version_num FROM alembic_version'))) == [actual_head]
            m.schema(db)
    finally:
        engine.dispose()
        with admin.begin() as db:
            db.execute(DropSchema(namespace, cascade=True))
        admin.dispose()


@pytest.mark.parametrize('revisions', [None, [], ['0054'], ['unknown'], ['current', '0054']])
def test_missing_stale_unknown_and_multiple_database_revisions_fail_closed(revisions):
    engine = create_engine('sqlite://')
    try:
        with Session(engine) as db:
            if revisions is not None:
                db.execute(text('CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)'))
                for revision in revisions:
                    db.execute(text('INSERT INTO alembic_version VALUES (:revision)'),
                               {'revision': m.SCHEMA if revision == 'current' else revision})
            with pytest.raises(ValueError, match='require schema'):
                m.schema(db)
    finally:
        engine.dispose()


@pytest.mark.parametrize('count', [0, 2])
def test_empty_or_branched_migration_package_is_rejected(tmp_path, count):
    versions = tmp_path / 'versions'
    versions.mkdir()
    for number in range(count):
        (versions / f'{number}.py').write_text(
            f'revision = "test_{number}"\ndown_revision = None\n', encoding='utf-8')
    with pytest.raises(RuntimeError, match='exactly one'):
        m.migration_schema(tmp_path)


def test_missing_migration_package_is_rejected(tmp_path):
    with pytest.raises(CommandError):
        m.migration_schema(tmp_path / 'missing')
