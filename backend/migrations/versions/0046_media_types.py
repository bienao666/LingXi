"""Workspace media admission formats; preserve all existing limits."""
from alembic import op
import sqlalchemy as sa

revision = '0046'
down_revision = '0045'
branch_labels = None
depends_on = None
DEFAULTS = '["text", "markdown", "csv", "pdf", "docx", "xlsx", "image", "qq_audio"]'


def upgrade():
    op.add_column('media_policies', sa.Column('allowed_types', sa.JSON(), nullable=False,
        server_default=sa.text("'" + DEFAULTS + "'")))
    op.alter_column('media_policies', 'allowed_types', server_default=None)


def downgrade():
    # Ignoring a restricted type list would expand access after downgrade.
    import json
    for value in op.get_bind().execute(sa.text('SELECT allowed_types FROM media_policies')).scalars():
        value = json.loads(value) if isinstance(value, str) else value
        if set(value) != set(json.loads(DEFAULTS)):
            raise RuntimeError('已有附件类型限制，不能有损降级；请恢复升级前备份与配套镜像')
    op.drop_column('media_policies', 'allowed_types')
