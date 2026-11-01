"""Owned attachments, message references and verified image capabilities."""
from alembic import op
import sqlalchemy as sa
revision = '0009'
down_revision = '0008'


def upgrade():
    op.create_table('media',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('request_id', sa.String(100), nullable=False),
        sa.Column('filename', sa.String(200), nullable=False),
        sa.Column('suffix', sa.String(10), nullable=False),
        sa.Column('size', sa.Integer(), nullable=False),
        sa.Column('digest', sa.String(64), nullable=False),
        sa.Column('status', sa.String(30), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('parsed', sa.JSON(), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('cleanup_status', sa.String(30), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.UniqueConstraint('user_id', 'request_id'))
    for field in ('workspace_id', 'user_id', 'status'):
        op.create_index('ix_media_' + field, 'media', [field])
    op.create_table('media_links',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('media_id', sa.String(64), nullable=False),
        sa.Column('message_id', sa.String(64), nullable=False),
        sa.UniqueConstraint('media_id', 'message_id'))
    for field in ('media_id', 'message_id'):
        op.create_index('ix_media_links_' + field, 'media_links', [field])
    op.create_table('model_vision',
        sa.Column('model_id', sa.String(64), primary_key=True),
        sa.Column('provider_version', sa.Integer(), nullable=False),
        sa.Column('model_name', sa.String(150), nullable=False),
        sa.Column('status', sa.String(30), nullable=False),
        sa.Column('checked_at', sa.Float(), nullable=False),
        sa.Column('error', sa.Text(), nullable=True))


def downgrade():
    op.drop_table('model_vision')
    op.drop_table('media_links')
    op.drop_table('media')
