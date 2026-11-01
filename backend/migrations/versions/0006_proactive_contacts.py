"""Source-bound topics, durable contact preferences and unique Web deliveries."""
from alembic import op
import sqlalchemy as sa

revision = '0006'
down_revision = '0005'


def entity():
    return [sa.Column('id', sa.String(64), primary_key=True),
            sa.Column('workspace_id', sa.String(64), nullable=False),
            sa.Column('created_at', sa.Float(), nullable=False)]


def upgrade():
    op.create_table('contact_preferences',
        sa.Column('user_id', sa.String(64), primary_key=True),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('mode', sa.String(30), nullable=False),
        sa.Column('timezone', sa.String(100), nullable=False),
        sa.Column('quiet_start', sa.String(5), nullable=False),
        sa.Column('quiet_end', sa.String(5), nullable=False),
        sa.Column('daily_limit', sa.Integer(), nullable=False),
        sa.Column('min_gap_hours', sa.Integer(), nullable=False),
        sa.Column('allowed_kinds', sa.JSON(), nullable=False),
        sa.Column('paused_until', sa.Float(), nullable=True),
        sa.Column('conversation_id', sa.String(64), nullable=True),
        sa.Column('blocked_sources', sa.JSON(), nullable=False))
    op.create_index('ix_contact_preferences_workspace_id', 'contact_preferences', ['workspace_id'])
    op.create_table('conversation_topics', *entity(),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('dedupe_key', sa.String(200), nullable=False),
        sa.Column('source_type', sa.String(20), nullable=False),
        sa.Column('source_id', sa.String(64), nullable=False),
        sa.Column('source_version', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(30), nullable=False),
        sa.Column('title', sa.String(200), nullable=False),
        sa.Column('not_before', sa.Float(), nullable=False),
        sa.Column('next_check_at', sa.Float(), nullable=False),
        sa.Column('expires_at', sa.Float(), nullable=False),
        sa.Column('conversation_id', sa.String(64), nullable=True),
        sa.Column('status', sa.String(30), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('origin', sa.String(30), nullable=False),
        sa.UniqueConstraint('user_id', 'dedupe_key'))
    for name in ('workspace_id', 'user_id', 'source_id', 'not_before', 'next_check_at', 'status'):
        op.create_index('ix_conversation_topics_'+name, 'conversation_topics', [name])
    op.create_table('proactive_contacts', *entity(),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('topic_id', sa.String(64), nullable=False, unique=True),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('status', sa.String(30), nullable=False),
        sa.Column('conversation_id', sa.String(64), nullable=True),
        sa.Column('message_id', sa.String(64), nullable=True),
        sa.Column('feedback', sa.String(30), nullable=True),
        sa.Column('feedback_at', sa.Float(), nullable=True),
        sa.Column('version', sa.Integer(), nullable=False))
    for name in ('workspace_id', 'user_id'):
        op.create_index('ix_proactive_contacts_'+name, 'proactive_contacts', [name])


def downgrade():
    for name in ('proactive_contacts', 'conversation_topics', 'contact_preferences'):
        op.drop_table(name)
