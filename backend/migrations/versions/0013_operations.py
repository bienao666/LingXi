"""Operational heartbeats, safe events, preferences and short-lived diagnostics."""
from alembic import op
import sqlalchemy as sa
revision='0013'
down_revision='0012'


def upgrade():
    op.create_table('worker_states',sa.Column('id',sa.String(64),primary_key=True),sa.Column('started_at',sa.Float(),nullable=False),
        sa.Column('seen_at',sa.Float(),nullable=False,index=True),sa.Column('slots',sa.Integer(),nullable=False),sa.Column('active',sa.Integer(),nullable=False))
    common=lambda:[sa.Column('id',sa.String(64),primary_key=True),sa.Column('workspace_id',sa.String(64),nullable=False,index=True),sa.Column('created_at',sa.Float(),nullable=False)]
    owner=lambda:[sa.Column('user_id',sa.String(64),nullable=False,index=True)]
    op.create_table('runtime_events',*common(),sa.Column('component',sa.String(30),nullable=False),sa.Column('code',sa.String(60),nullable=False),
        sa.Column('level',sa.String(20),nullable=False),sa.Column('reference',sa.String(64)))
    op.create_table('ops_preferences',*common(),*owner(),sa.Column('version',sa.Integer(),nullable=False),sa.Column('log_retention_days',sa.Integer(),nullable=False),
        sa.Column('daily_token_alert',sa.Integer(),nullable=False),sa.UniqueConstraint('user_id','workspace_id'))
    op.create_table('diagnostic_bundles',*common(),*owner(),sa.Column('session_id',sa.String(64),nullable=False),sa.Column('request_id',sa.String(100),nullable=False),
        sa.Column('request_hash',sa.String(64),nullable=False),sa.Column('payload',sa.Text(),nullable=False),sa.Column('digest',sa.String(64),nullable=False),
        sa.Column('status',sa.String(20),nullable=False),sa.Column('expires_at',sa.Float(),nullable=False),sa.UniqueConstraint('user_id','workspace_id','request_id'))


def downgrade():
    for table in ('diagnostic_bundles','ops_preferences','runtime_events','worker_states'):op.drop_table(table)
