"""Atomic cross-domain configuration history, without credentials or grants."""
from alembic import op
import sqlalchemy as sa

revision = '0060'
down_revision = '0059'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('configuration_revisions',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('number', sa.Integer(), nullable=False),
        sa.Column('action', sa.String(20), nullable=False),
        sa.Column('modules', sa.JSON(), nullable=False),
        sa.Column('before_snapshot', sa.JSON(), nullable=False),
        sa.Column('after_snapshot', sa.JSON(), nullable=False),
        sa.Column('source_id', sa.String(64), nullable=True),
        sa.Column('source_point', sa.String(10), nullable=True),
        sa.UniqueConstraint('workspace_id', 'user_id', 'number'))
    for field in ('workspace_id', 'user_id'):
        op.create_index('ix_configuration_revisions_'+field, 'configuration_revisions', [field])


def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM configuration_revisions')).scalar():
        raise RuntimeError('已有配置历史，请使用升级前备份与配套镜像整体回退')
    op.drop_table('configuration_revisions')
