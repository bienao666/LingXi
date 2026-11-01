"""QQ document metadata resolution, scoped to the original authenticated input."""
from alembic import op
import sqlalchemy as sa

revision = '0034'
down_revision = '0033'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('channel_attachments', sa.Column('document_state', sa.JSON(), nullable=False, server_default='{}'))
    op.alter_column('channel_attachments', 'document_state', server_default=None)


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM channel_attachments WHERE CAST(document_state AS TEXT) != '{}'")):
        raise RuntimeError('已有 QQ 文档取址记录；请恢复升级前备份和镜像，不能丢弃取址状态')
    op.drop_column('channel_attachments', 'document_state')
