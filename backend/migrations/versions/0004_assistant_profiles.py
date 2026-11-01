"""Versioned personas, roles, agent configurations and support preferences."""
from alembic import op
import sqlalchemy as sa

revision = '0004'
down_revision = '0003'


def entity_columns():
    return [sa.Column('id', sa.String(64), primary_key=True),
            sa.Column('workspace_id', sa.String(64), nullable=False),
            sa.Column('created_at', sa.Float(), nullable=False)]


def upgrade():
    op.create_table('assistant_profiles', *entity_columns(),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('kind', sa.String(20), nullable=False),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('description', sa.String(500), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('spec', sa.JSON(), nullable=False),
        sa.Column('updated_at', sa.Float(), nullable=False))
    for col in ('workspace_id', 'user_id', 'kind'):
        op.create_index('ix_assistant_profiles_'+col, 'assistant_profiles', [col])
    op.create_table('assistant_profile_revisions', *entity_columns(),
        sa.Column('profile_id', sa.String(64), nullable=False),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('snapshot', sa.JSON(), nullable=False),
        sa.UniqueConstraint('profile_id', 'version'))
    for col in ('workspace_id', 'profile_id', 'user_id'):
        op.create_index('ix_assistant_profile_revisions_'+col, 'assistant_profile_revisions', [col])
    op.create_table('conversation_personas',
        sa.Column('conversation_id', sa.String(64), primary_key=True),
        sa.Column('persona_id', sa.String(64), nullable=True),
        sa.Column('version', sa.Integer(), nullable=False))
    op.create_table('support_preferences',
        sa.Column('user_id', sa.String(64), primary_key=True),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('spec', sa.JSON(), nullable=False),
        sa.Column('updated_at', sa.Float(), nullable=False))
    op.create_index('ix_support_preferences_workspace_id', 'support_preferences', ['workspace_id'])


def downgrade():
    for table in ('support_preferences', 'conversation_personas', 'assistant_profile_revisions', 'assistant_profiles'):
        op.drop_table(table)
