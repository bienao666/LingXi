"""Versioned manual token rates and immutable call cost snapshots."""
from alembic import op
import sqlalchemy as sa

revision = '0040'
down_revision = '0039'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('model_prices',
        sa.Column('model_id', sa.String(64), primary_key=True),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('config', sa.JSON(), nullable=False))
    op.create_index('ix_model_prices_workspace_id', 'model_prices', ['workspace_id'])
    op.add_column('model_calls', sa.Column('billing', sa.JSON(), nullable=False, server_default='{}'))


def downgrade():
    db = op.get_bind()
    if db.execute(sa.text('SELECT count(*) FROM model_prices')).scalar():
        raise RuntimeError('已有价格配置，不能丢弃计价历史；请恢复升级前备份和配套镜像')
    # Text comparison supports both PostgreSQL JSON and SQLite migration checks.
    if db.execute(sa.text("SELECT count(*) FROM model_calls WHERE CAST(billing AS TEXT) != '{}' ")).scalar():
        raise RuntimeError('已有调用费用记录，不能丢弃计价历史；请恢复升级前备份和配套镜像')
    op.drop_column('model_calls', 'billing')
    op.drop_table('model_prices')
