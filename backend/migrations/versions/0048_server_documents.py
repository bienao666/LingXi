"""Separate document grants and source-fenced media snapshots; no implicit upgrade grant."""
from alembic import op
import sqlalchemy as sa

revision = '0048'
down_revision = '0047'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('resource_policies', sa.Column('read_roots', sa.JSON(), nullable=False, server_default='[]'))
    op.add_column('media', sa.Column('source', sa.JSON(), nullable=False, server_default='{}'))


def downgrade():
    db = op.get_bind()
    if (db.scalar(sa.text("SELECT count(*) FROM resource_policies WHERE CAST(read_roots AS TEXT) != '[]'"))
            or db.scalar(sa.text("SELECT count(*) FROM media WHERE CAST(source AS TEXT) != '{}'"))):
        raise RuntimeError('已有正文授权或来源快照，不能移除来源检查；请恢复配套备份')
    op.drop_column('media', 'source')
    op.drop_column('resource_policies', 'read_roots')
