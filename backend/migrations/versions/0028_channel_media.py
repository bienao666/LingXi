"""QQ image and document ingestion through the authoritative media lifecycle."""
from alembic import op
import sqlalchemy as sa

revision = '0028'
down_revision = '0027'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('channel_attachments',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('inbound_id', sa.String(64), nullable=False),
        sa.Column('part', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(20), nullable=False),
        sa.Column('filename', sa.String(200), nullable=False),
        sa.Column('encrypted_url', sa.Text(), nullable=False),
        sa.Column('expires_at', sa.Float(), nullable=False),
        sa.Column('media_id', sa.String(64), nullable=True),
        sa.Column('conversation_id', sa.String(64), nullable=True),
        sa.Column('status', sa.String(30), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.UniqueConstraint('inbound_id', 'part'))
    for name in ('workspace_id', 'inbound_id', 'media_id'):
        op.create_index('ix_channel_attachments_'+name, 'channel_attachments', [name])


def downgrade():
    if op.get_bind().scalar(sa.text('SELECT count(*) FROM channel_attachments')):
        raise RuntimeError('已有 QQ 附件接收记录；请恢复升级前备份及配套镜像，不能丢弃来源和删除屏障')
    op.drop_table('channel_attachments')
