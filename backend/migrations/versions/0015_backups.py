"""Host-operated backup requests, schedules and audit metadata."""
from alembic import op
import sqlalchemy as sa
revision='0015'
down_revision='0014'


def upgrade():
    common=lambda:[sa.Column('id',sa.String(64),primary_key=True),sa.Column('workspace_id',sa.String(64),nullable=False,index=True),sa.Column('created_at',sa.Float(),nullable=False),sa.Column('user_id',sa.String(64),nullable=False,index=True)]
    op.create_table('backup_policies',*common(),sa.Column('version',sa.Integer(),nullable=False),sa.Column('enabled',sa.Boolean(),nullable=False),sa.Column('interval_hours',sa.Integer(),nullable=False),sa.Column('next_at',sa.Float(),nullable=False),sa.UniqueConstraint('user_id','workspace_id'))
    op.create_table('backup_runs',*common(),sa.Column('request_id',sa.String(100),nullable=False),sa.Column('kind',sa.String(20),nullable=False),sa.Column('archive_id',sa.String(64),nullable=False),sa.Column('status',sa.String(20),nullable=False,index=True),sa.Column('finished_at',sa.Float(),nullable=False),sa.Column('digest',sa.String(64),nullable=False),sa.Column('bytes',sa.BigInteger(),nullable=False),sa.Column('error',sa.String(40),nullable=False),sa.UniqueConstraint('user_id','workspace_id','request_id'))
    op.create_table('backup_agents',sa.Column('id',sa.String(64),primary_key=True),sa.Column('seen_at',sa.Float(),nullable=False))


def downgrade():
    if op.get_bind().execute(sa.text("select count(*) from backup_runs where status='running'")).scalar():raise RuntimeError('Finish or recover running backup before downgrade')
    for table in ('backup_agents','backup_runs','backup_policies'):op.drop_table(table)
