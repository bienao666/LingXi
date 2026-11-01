"""Source-fenced chat dispatch links, separate from task execution facts."""
from alembic import op
import sqlalchemy as sa
revision = '0017'
down_revision = '0016'


def upgrade():
    op.create_table('chat_actions',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('run_id', sa.String(64), nullable=False, unique=True),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('conversation_id', sa.String(64), nullable=False),
        sa.Column('source_id', sa.String(64), nullable=False),
        sa.Column('capability', sa.String(100), nullable=False),
        sa.Column('target_id', sa.String(64), nullable=False, unique=True),
        sa.Column('status', sa.String(30), nullable=False))
    for field in ('workspace_id', 'user_id', 'conversation_id'):
        op.create_index('ix_chat_actions_' + field, 'chat_actions', [field])


def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM chat_actions')).scalar():
        raise RuntimeError('Chat task source fences must be retained; restore the full pre-upgrade backup to roll back')
    op.drop_table('chat_actions')
