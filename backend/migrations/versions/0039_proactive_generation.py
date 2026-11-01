"""Opt-in bounded suggestions with durable execution barriers."""
from alembic import op
import sqlalchemy as sa

revision = '0039'
down_revision = '0038'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('contact_preferences', sa.Column('model_generation', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table('proactive_drafts',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('user_id', sa.String(64), nullable=False), sa.Column('topic_id', sa.String(64), nullable=False, unique=True),
        sa.Column('status', sa.String(30), nullable=False), sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('attempt', sa.Integer(), nullable=False), sa.Column('config', sa.JSON(), nullable=False),
        sa.Column('content', sa.Text(), nullable=False), sa.Column('error', sa.Text(), nullable=True))
    for name in ('workspace_id', 'user_id'):
        op.create_index('ix_proactive_drafts_'+name, 'proactive_drafts', [name])


def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM proactive_drafts')).scalar():
        raise RuntimeError('已有构思执行屏障，不能丢弃后自动重复计费；请恢复升级前备份及镜像')
    op.drop_table('proactive_drafts')
    op.drop_column('contact_preferences', 'model_generation')
