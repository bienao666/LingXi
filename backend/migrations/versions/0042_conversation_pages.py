"""Stable keyset pagination indexes; no business data changes."""
from alembic import op

revision = '0042'
down_revision = '0041'
branch_labels = None
depends_on = None


def upgrade():
    op.create_index('ix_conversations_page', 'conversations', ['user_id', 'workspace_id', 'deleted', 'created_at', 'id'])
    op.create_index('ix_messages_page', 'messages', ['conversation_id', 'workspace_id', 'created_at', 'id'])


def downgrade():
    op.drop_index('ix_messages_page', table_name='messages')
    op.drop_index('ix_conversations_page', table_name='conversations')
