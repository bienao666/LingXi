"""Explicit per-reminder QQ destination and durable protocol receipts."""
from alembic import op
import sqlalchemy as sa

revision = '0059'
down_revision = '0058'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('reminder_deliveries',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('reminder_id', sa.String(64), nullable=False, unique=True),
        sa.Column('reminder_version', sa.Integer(), nullable=False),
        sa.Column('source_hash', sa.String(64), nullable=False),
        sa.Column('channel_id', sa.String(64), nullable=True),
        sa.Column('target', sa.JSON(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(30), nullable=False),
        sa.Column('expires_at', sa.Float(), nullable=False),
        sa.Column('confirmation_hash', sa.String(64), nullable=False),
        sa.Column('echo', sa.String(100), nullable=True, unique=True),
        sa.Column('sent_at', sa.Float(), nullable=True),
        sa.Column('protocol_message_id', sa.String(30), nullable=True),
        sa.Column('reviewed_at', sa.Float(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True))
    for name in ('workspace_id', 'user_id', 'channel_id'):
        op.create_index('ix_reminder_deliveries_' + name, 'reminder_deliveries', [name])


def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM reminder_deliveries')).scalar():
        raise RuntimeError('已有提醒投递屏障，请使用升级前备份与配套镜像整体回退')
    op.drop_table('reminder_deliveries')
