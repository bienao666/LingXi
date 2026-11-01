"""Versioned, review-only memory proposals from bounded delegation."""
from alembic import op
import sqlalchemy as sa

revision = '0038'
down_revision = '0037'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('memory_candidates', sa.Column('origin',sa.JSON(),nullable=False,server_default='{}'))
    op.add_column('memories', sa.Column('provenance',sa.JSON(),nullable=False,server_default='{}'))


def downgrade():
    bind=op.get_bind()
    if any(value not in (None, {}, '{}') for value in bind.execute(sa.text('SELECT origin FROM memory_candidates')).scalars()):
        raise RuntimeError('已有协作记忆来源，须恢复升级前数据库及镜像，不能丢弃来源失效屏障')
    if any(value not in (None, {}, '{}') for value in bind.execute(sa.text('SELECT provenance FROM memories')).scalars()):
        raise RuntimeError('已有派生记忆，不能丢弃依赖来源')
    op.drop_column('memory_candidates','origin')
    op.drop_column('memories','provenance')
