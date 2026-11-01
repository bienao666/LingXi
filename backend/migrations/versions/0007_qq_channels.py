"""QQ OneBot instances, verified identity and durable message receipts."""
from alembic import op
import sqlalchemy as sa
revision = '0007'
down_revision = '0006'

def upgrade():
    op.create_table('channels',
        sa.Column('user_id', sa.String(length=64), nullable=False, primary_key=False),
        sa.Column('name', sa.String(length=100), nullable=False, primary_key=False),
        sa.Column('bot_id', sa.String(length=30), nullable=False, primary_key=False),
        sa.Column('encrypted_token', sa.Text(), nullable=False, primary_key=False),
        sa.Column('version', sa.Integer(), nullable=False, primary_key=False),
        sa.Column('enabled', sa.Boolean(), nullable=False, primary_key=False),
        sa.Column('rules', sa.JSON(), nullable=False, primary_key=False),
        sa.Column('connection_id', sa.String(length=64), nullable=True, primary_key=False),
        sa.Column('lease_until', sa.Float(), nullable=False, primary_key=False),
        sa.Column('last_seen', sa.Float(), nullable=True, primary_key=False),
        sa.Column('login_state', sa.String(length=30), nullable=False, primary_key=False),
        sa.Column('protocol_version', sa.String(length=100), nullable=False, primary_key=False),
        sa.Column('capabilities', sa.JSON(), nullable=False, primary_key=False),
        sa.Column('last_sent', sa.Float(), nullable=False, primary_key=False),
        sa.Column('id', sa.String(length=64), nullable=False, primary_key=True),
        sa.Column('workspace_id', sa.String(length=64), nullable=False, primary_key=False),
        sa.Column('created_at', sa.Float(), nullable=False, primary_key=False),
    )
    op.create_index('ix_channels_user_id','channels',['user_id'])
    op.create_index('ix_channels_workspace_id','channels',['workspace_id'])
    op.create_table('identity_links',
        sa.Column('user_id', sa.String(length=64), nullable=False, primary_key=False),
        sa.Column('channel_id', sa.String(length=64), nullable=False, primary_key=False),
        sa.Column('external_user_id', sa.String(length=30), nullable=False, primary_key=False),
        sa.Column('status', sa.String(length=30), nullable=False, primary_key=False),
        sa.Column('challenge_hash', sa.String(length=64), nullable=False, primary_key=False),
        sa.Column('expires_at', sa.Float(), nullable=False, primary_key=False),
        sa.Column('allow_memory', sa.Boolean(), nullable=False, primary_key=False),
        sa.Column('version', sa.Integer(), nullable=False, primary_key=False),
        sa.Column('id', sa.String(length=64), nullable=False, primary_key=True),
        sa.Column('workspace_id', sa.String(length=64), nullable=False, primary_key=False),
        sa.Column('created_at', sa.Float(), nullable=False, primary_key=False),
        sa.UniqueConstraint('channel_id','external_user_id'),
    )
    op.create_index('ix_identity_links_channel_id','identity_links',['channel_id'])
    op.create_index('ix_identity_links_user_id','identity_links',['user_id'])
    op.create_index('ix_identity_links_workspace_id','identity_links',['workspace_id'])
    op.create_table('channel_sessions',
        sa.Column('user_id', sa.String(length=64), nullable=False, primary_key=False),
        sa.Column('channel_id', sa.String(length=64), nullable=False, primary_key=False),
        sa.Column('scope_key', sa.String(length=200), nullable=False, primary_key=False),
        sa.Column('actor_id', sa.String(length=64), nullable=False, primary_key=False),
        sa.Column('conversation_id', sa.String(length=64), nullable=False, primary_key=False),
        sa.Column('id', sa.String(length=64), nullable=False, primary_key=True),
        sa.Column('workspace_id', sa.String(length=64), nullable=False, primary_key=False),
        sa.Column('created_at', sa.Float(), nullable=False, primary_key=False),
        sa.UniqueConstraint('channel_id','scope_key'),
    )
    op.create_index('ix_channel_sessions_channel_id','channel_sessions',['channel_id'])
    op.create_index('ix_channel_sessions_workspace_id','channel_sessions',['workspace_id'])
    op.create_table('channel_inbounds',
        sa.Column('user_id', sa.String(length=64), nullable=False, primary_key=False),
        sa.Column('channel_id', sa.String(length=64), nullable=False, primary_key=False),
        sa.Column('channel_version', sa.Integer(), nullable=False, primary_key=False),
        sa.Column('external_message_id', sa.String(length=60), nullable=False, primary_key=False),
        sa.Column('target_key', sa.String(length=100), nullable=False, primary_key=False),
        sa.Column('sender_id', sa.String(length=30), nullable=False, primary_key=False),
        sa.Column('content', sa.Text(), nullable=False, primary_key=False),
        sa.Column('binding_id', sa.String(length=64), nullable=True, primary_key=False),
        sa.Column('binding_version', sa.Integer(), nullable=True, primary_key=False),
        sa.Column('status', sa.String(length=30), nullable=False, primary_key=False),
        sa.Column('run_id', sa.String(length=64), nullable=True, primary_key=False),
        sa.Column('error', sa.Text(), nullable=True, primary_key=False),
        sa.Column('id', sa.String(length=64), nullable=False, primary_key=True),
        sa.Column('workspace_id', sa.String(length=64), nullable=False, primary_key=False),
        sa.Column('created_at', sa.Float(), nullable=False, primary_key=False),
        sa.UniqueConstraint('channel_id','target_key','external_message_id'),
    )
    op.create_index('ix_channel_inbounds_channel_id','channel_inbounds',['channel_id'])
    op.create_index('ix_channel_inbounds_status','channel_inbounds',['status'])
    op.create_index('ix_channel_inbounds_target_key','channel_inbounds',['target_key'])
    op.create_index('ix_channel_inbounds_user_id','channel_inbounds',['user_id'])
    op.create_index('ix_channel_inbounds_workspace_id','channel_inbounds',['workspace_id'])
    op.create_table('channel_deliveries',
        sa.Column('user_id', sa.String(length=64), nullable=False, primary_key=False),
        sa.Column('channel_id', sa.String(length=64), nullable=False, primary_key=False),
        sa.Column('inbound_id', sa.String(length=64), nullable=False, primary_key=False),
        sa.Column('part', sa.Integer(), nullable=False, primary_key=False),
        sa.Column('content', sa.Text(), nullable=False, primary_key=False),
        sa.Column('status', sa.String(length=30), nullable=False, primary_key=False),
        sa.Column('echo', sa.String(length=100), nullable=False, primary_key=False),
        sa.Column('sent_at', sa.Float(), nullable=True, primary_key=False),
        sa.Column('protocol_message_id', sa.String(length=60), nullable=True, primary_key=False),
        sa.Column('error', sa.Text(), nullable=True, primary_key=False),
        sa.Column('reviewed_at', sa.Float(), nullable=True, primary_key=False),
        sa.Column('id', sa.String(length=64), nullable=False, primary_key=True),
        sa.Column('workspace_id', sa.String(length=64), nullable=False, primary_key=False),
        sa.Column('created_at', sa.Float(), nullable=False, primary_key=False),
        sa.UniqueConstraint('echo'),
        sa.UniqueConstraint('inbound_id','part'),
    )
    op.create_index('ix_channel_deliveries_channel_id','channel_deliveries',['channel_id'])
    op.create_index('ix_channel_deliveries_inbound_id','channel_deliveries',['inbound_id'])
    op.create_index('ix_channel_deliveries_status','channel_deliveries',['status'])
    op.create_index('ix_channel_deliveries_user_id','channel_deliveries',['user_id'])
    op.create_index('ix_channel_deliveries_workspace_id','channel_deliveries',['workspace_id'])

def downgrade():
    op.drop_table('channel_deliveries')
    op.drop_table('channel_inbounds')
    op.drop_table('channel_sessions')
    op.drop_table('identity_links')
    op.drop_table('channels')
