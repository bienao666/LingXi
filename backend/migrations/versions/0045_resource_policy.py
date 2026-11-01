"""Explicit owner-scoped server directory grants; legacy mount is not a grant."""
from alembic import op
import sqlalchemy as sa

revision = '0045'
down_revision = '0044'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('resource_policies',
        sa.Column('workspace_id', sa.String(64), primary_key=True),
        sa.Column('user_id', sa.String(64), primary_key=True),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('roots', sa.JSON(), nullable=False),
        sa.Column('updated_at', sa.Float(), nullable=False))


def downgrade():
    if op.get_bind().scalar(sa.text('SELECT count(*) FROM resource_policies')):
        raise RuntimeError('已有资源授权记录，不能有损降级；请恢复升级前备份与配套镜像')
    op.drop_table('resource_policies')
