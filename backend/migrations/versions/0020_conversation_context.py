"""Source-fenced, per-run context compaction snapshots."""
from alembic import op
import sqlalchemy as sa

revision = '0020'
down_revision = '0019'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('conversation_contexts',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('conversation_id', sa.String(64), nullable=False),
        sa.Column('run_id', sa.String(64), nullable=False, unique=True),
        sa.Column('status', sa.String(30), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('source_hashes', sa.JSON(), nullable=False),
        sa.Column('counts', sa.JSON(), nullable=False),
        sa.Column('data', sa.JSON(), nullable=False))
    for column in ('workspace_id', 'user_id', 'conversation_id'):
        op.create_index('ix_conversation_contexts_' + column, 'conversation_contexts', [column])


def downgrade():
    if op.get_bind().scalar(sa.text('SELECT count(*) FROM conversation_contexts')):
        raise RuntimeError('仍有上下文摘要及来源记录；请恢复升级前备份及配套镜像，不可直接降级丢失依据')
    op.drop_table('conversation_contexts')
