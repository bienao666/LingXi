"""Opt-in unused upload collection and independent file protection."""
from alembic import op
import sqlalchemy as sa

revision = '0051'
down_revision = '0050'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('media_retention_policies',
        sa.Column('user_id', sa.String(64), primary_key=True),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('version', sa.Integer, nullable=False),
        sa.Column('enabled', sa.Boolean, nullable=False),
        sa.Column('days', sa.Integer, nullable=False),
        sa.Column('last_checked', sa.Float, nullable=False),
        sa.Column('last_marked', sa.Integer, nullable=False))
    op.create_index('ix_media_retention_policies_workspace_id', 'media_retention_policies', ['workspace_id'])
    op.create_table('media_protections',
        sa.Column('media_id', sa.String(64), primary_key=True),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('version', sa.Integer, nullable=False),
        sa.Column('protected', sa.Boolean, nullable=False))
    for key in ('user_id', 'workspace_id'):
        op.create_index('ix_media_protections_'+key, 'media_protections', [key])
    op.create_table('media_retention_runs',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('created_at', sa.Float, nullable=False),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('request_hash', sa.String(64), nullable=False),
        sa.Column('kind', sa.String(20), nullable=False),
        sa.Column('marked', sa.Integer, nullable=False),
        sa.Column('skipped', sa.Integer, nullable=False),
        sa.Column('bytes', sa.Integer, nullable=False))
    for key in ('user_id', 'workspace_id'):
        op.create_index('ix_media_retention_runs_'+key, 'media_retention_runs', [key])


def downgrade():
    db = op.get_bind()
    for name in ('media_retention_policies', 'media_protections', 'media_retention_runs'):
        if db.execute(sa.text('SELECT count(*) FROM '+name)).scalar():
            raise RuntimeError('已有回收配置、保留设置或回执，请使用升级前备份与配套镜像回退')
    for name in ('media_retention_runs', 'media_protections', 'media_retention_policies'):
        op.drop_table(name)
