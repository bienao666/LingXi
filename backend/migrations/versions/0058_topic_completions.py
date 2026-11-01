"""Keep explicit topic completion across source corrections and old restores."""
from alembic import op
import sqlalchemy as sa

revision = '0058'
down_revision = '0057'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('topic_completions',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('source_type', sa.String(20), nullable=False),
        sa.Column('source_id', sa.String(64), nullable=False),
        sa.Column('source_version', sa.Integer(), nullable=False),
        sa.Column('topic_id', sa.String(64), nullable=False),
        sa.Column('completed_at', sa.Float(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False))
    for name in ('workspace_id', 'user_id'):
        op.create_index('ix_topic_completions_' + name, 'topic_completions', [name])


def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM topic_completions')).scalar():
        raise RuntimeError('已有话题收尾屏障，请使用升级前备份与配套镜像整体回退')
    op.drop_table('topic_completions')
