"""Bounded media spool scans and durable explicit cleanup receipts."""
from alembic import op
import sqlalchemy as sa

revision = '0054'
down_revision = '0053'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('media_storage_runs',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('created_at', sa.Float, nullable=False),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('login_id', sa.String(64), nullable=False),
        sa.Column('request_id', sa.String(100), nullable=False),
        sa.Column('request_hash', sa.String(64), nullable=False),
        sa.Column('days', sa.Integer, nullable=False),
        sa.Column('status', sa.String(30), nullable=False),
        sa.Column('plan', sa.JSON, nullable=False),
        sa.Column('results', sa.JSON, nullable=False),
        sa.Column('expires_at', sa.Float, nullable=False),
        sa.Column('error', sa.Text, nullable=True),
        sa.Column('finished_at', sa.Float, nullable=True),
        sa.UniqueConstraint('user_id', 'request_id'))
    for key in ('workspace_id', 'user_id'):
        op.create_index('ix_media_storage_runs_'+key, 'media_storage_runs', [key])


def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM media_storage_runs')).scalar():
        raise RuntimeError('已有存储巡检或清理屏障，请使用升级前备份和配套镜像回退')
    op.drop_table('media_storage_runs')
