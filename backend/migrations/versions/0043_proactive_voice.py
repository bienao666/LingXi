"""Explicit per-session consent and durable proactive speech receipts."""
from alembic import op
import sqlalchemy as sa

revision = '0043'
down_revision = '0042'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('voice_sessions', sa.Column('allow_proactive', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table('voice_contacts',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('contact_id', sa.String(64), nullable=False, unique=True),
        sa.Column('message_id', sa.String(64), nullable=False, unique=True),
        sa.Column('session_id', sa.String(64), nullable=False),
        sa.Column('playback_id', sa.String(64), nullable=True, unique=True),
        sa.Column('status', sa.String(30), nullable=False),
        sa.Column('expires_at', sa.Float(), nullable=False),
        sa.Column('preference_version', sa.Integer(), nullable=False),
        sa.Column('speech', sa.JSON(), nullable=False))
    for column in ('workspace_id', 'user_id', 'session_id'):
        op.create_index('ix_voice_contacts_'+column, 'voice_contacts', [column])


def downgrade():
    bind = op.get_bind()
    if bind.scalar(sa.text('SELECT count(*) FROM voice_contacts')) or bind.scalar(sa.text('SELECT count(*) FROM voice_sessions WHERE allow_proactive')):
        raise RuntimeError('已有主动语音授权或回执，降级会丢失播放事实；请恢复升级前备份及配套镜像')
    op.drop_table('voice_contacts')
    op.drop_column('voice_sessions', 'allow_proactive')
