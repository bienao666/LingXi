"""Explicit voice configuration, sessions and conservative playback receipts."""
from alembic import op
import sqlalchemy as sa
revision = '0010'
down_revision = '0009'


def upgrade():
    op.create_table('voice_config', sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('workspace_id', sa.String(64), nullable=False), sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('spec', sa.JSON(), nullable=False), sa.Column('checks', sa.JSON(), nullable=False))
    op.create_table('voice_sessions', sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('workspace_id', sa.String(64), nullable=False), sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('login_session_id', sa.String(64), nullable=False), sa.Column('conversation_id', sa.String(64), nullable=False),
        sa.Column('config_version', sa.Integer(), nullable=False), sa.Column('status', sa.String(30), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False), sa.Column('expires_at', sa.Float(), nullable=False),
        sa.Column('heartbeat_at', sa.Float(), nullable=False))
    for name in ('user_id', 'conversation_id'):
        op.create_index('ix_voice_sessions_' + name, 'voice_sessions', [name])
    op.create_table('voice_playbacks', sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('workspace_id', sa.String(64), nullable=False), sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('session_id', sa.String(64), nullable=False), sa.Column('message_id', sa.String(64), nullable=False),
        sa.Column('digest', sa.String(64), nullable=False), sa.Column('confirmed_parts', sa.Integer(), nullable=False),
        sa.Column('offered_parts', sa.Integer(), nullable=False), sa.Column('status', sa.String(30), nullable=False),
        sa.UniqueConstraint('session_id', 'message_id'))
    for name in ('user_id', 'message_id'):
        op.create_index('ix_voice_playbacks_' + name, 'voice_playbacks', [name])


def downgrade():
    op.drop_table('voice_playbacks')
    op.drop_table('voice_sessions')
    op.drop_table('voice_config')
