"""Reviewed workspace defaults and append-only configuration history."""
from alembic import op
import sqlalchemy as sa

revision = '0044'
down_revision = '0043'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('conversation_personas', sa.Column('inherit_default', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table('workspace_settings',
        sa.Column('workspace_id', sa.String(64), primary_key=True),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('values', sa.JSON(), nullable=False),
        sa.Column('updated_at', sa.Float(), nullable=False))
    op.create_table('settings_revisions',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('values', sa.JSON(), nullable=False),
        sa.Column('action', sa.String(20), nullable=False),
        sa.Column('source_version', sa.Integer(), nullable=True),
        sa.UniqueConstraint('workspace_id', 'version'))
    op.create_index('ix_settings_revisions_workspace_id', 'settings_revisions', ['workspace_id'])


def downgrade():
    bind = op.get_bind()
    if (bind.scalar(sa.text('SELECT count(*) FROM settings_revisions')) or bind.scalar(sa.text('SELECT count(*) FROM workspace_settings'))
            or bind.scalar(sa.text('SELECT count(*) FROM conversation_personas WHERE inherit_default'))):
        raise RuntimeError('已有系统配置或历史，不能有损降级；请恢复升级前备份与配套镜像')
    op.drop_table('settings_revisions')
    op.drop_table('workspace_settings')
    op.drop_column('conversation_personas', 'inherit_default')
