"""Workspace-scoped attachment admission limits."""
from alembic import op
import sqlalchemy as sa

revision = '0041'
down_revision = '0040'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('media_policies',
        sa.Column('workspace_id', sa.String(64), primary_key=True),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('max_files', sa.Integer(), nullable=False),
        sa.Column('max_file_mib', sa.Integer(), nullable=False),
        sa.Column('max_total_mib', sa.Integer(), nullable=False))
    # Existing workspaces have an immediately lockable row for QQ/HTTP commit
    # admission. Settings retain the original defaults; no user assets change.
    op.execute(sa.text('INSERT INTO media_policies '
        '(workspace_id, version, max_files, max_file_mib, max_total_mib) '
        'SELECT DISTINCT workspace_id, 1, 10, 20, 50 FROM users'))


def downgrade():
    count = op.get_bind().execute(sa.text('SELECT count(*) FROM media_policies '
        'WHERE version > 1 OR max_files != 10 OR max_file_mib != 20 OR max_total_mib != 50')).scalar()
    if count:
        raise RuntimeError('附件策略已修改，降级将扩大接收范围；请恢复升级前备份与配套镜像')
    op.drop_table('media_policies')
