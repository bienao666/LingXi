"""Frozen conversation prefixes; original messages remain authoritative."""
from alembic import op
import sqlalchemy as sa

revision = '0019'
down_revision = '0018'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('conversation_branches',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('conversation_id', sa.String(64), nullable=False, unique=True),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('parent_id', sa.String(64), nullable=False),
        sa.Column('anchor_id', sa.String(64), nullable=False),
        sa.Column('mode', sa.String(20), nullable=False),
        sa.Column('request_id', sa.String(100), nullable=False),
        sa.Column('request_hash', sa.String(64), nullable=False),
        sa.Column('prefix_ids', sa.JSON(), nullable=False),
        sa.Column('run_id', sa.String(64), nullable=False),
        sa.UniqueConstraint('user_id', 'request_id'))
    for column in ('workspace_id', 'user_id', 'parent_id'):
        op.create_index('ix_conversation_branches_' + column, 'conversation_branches', [column])


def downgrade():
    if op.get_bind().scalar(sa.text('SELECT count(*) FROM conversation_branches')):
        raise RuntimeError('仍有消息分支；降级会丢失原文范围，请恢复升级前备份及配套镜像')
    op.drop_table('conversation_branches')
