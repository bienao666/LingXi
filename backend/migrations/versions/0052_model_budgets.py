"""Durable upstream model request admission counters."""
from alembic import op
import sqlalchemy as sa

revision = '0052'
down_revision = '0051'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('model_budgets',
        sa.Column('workspace_id', sa.String(64), primary_key=True),
        sa.Column('version', sa.Integer, nullable=False),
        sa.Column('daily_limit', sa.Integer, nullable=False),
        sa.Column('execution_limit', sa.Integer, nullable=False),
        sa.Column('confirmed_hash', sa.String(64), nullable=False))
    op.create_table('model_dispatches',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('created_at', sa.Float, nullable=False),
        sa.Column('provider_id', sa.String(64), nullable=False),
        sa.Column('root_kind', sa.String(20), nullable=False),
        sa.Column('root_id', sa.String(64), nullable=False),
        sa.Column('policy_version', sa.Integer, nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('call_id', sa.String(64), nullable=True))
    op.create_index('ix_model_dispatches_daily', 'model_dispatches', ['workspace_id', 'created_at'])
    op.create_index('ix_model_dispatches_execution', 'model_dispatches', ['workspace_id', 'root_kind', 'root_id'])


def downgrade():
    for table in ('model_dispatches', 'model_budgets'):
        if op.get_bind().execute(sa.text('SELECT count(*) FROM '+table)).scalar():
            raise RuntimeError('已有调用限额或请求记录，请使用升级前备份和配套镜像回退')
    op.drop_table('model_dispatches')
    op.drop_table('model_budgets')
