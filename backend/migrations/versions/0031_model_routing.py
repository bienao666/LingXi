"""Explicit, endpoint-pinned ordinary-chat routing approval."""
from alembic import op
import sqlalchemy as sa

revision = '0031'
down_revision = '0030'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('model_routing',
        sa.Column('workspace_id', sa.String(64), primary_key=True),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('config', sa.JSON(), nullable=False))


def downgrade():
    if op.get_bind().scalar(sa.text('SELECT count(*) FROM model_routing')):
        raise RuntimeError('已有路由授权；请恢复升级前备份，不可丢弃数据接收范围和版本屏障')
    op.drop_table('model_routing')
