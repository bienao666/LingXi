"""QQ answer-file provenance and durable protocol upload receipts."""
from alembic import op
import sqlalchemy as sa

revision = '0033'
down_revision = '0032'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('channel_deliveries', sa.Column('file_info', sa.JSON(), nullable=False, server_default='{}'))
    op.add_column('channel_deliveries', sa.Column('protocol_file_id', sa.String(200), nullable=True))
    op.alter_column('channel_deliveries', 'file_info', server_default=None)


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM channel_deliveries WHERE kind = 'file'")):
        raise RuntimeError('已有 QQ 文件投递记录；请恢复升级前备份和镜像，不能丢弃文件投递屏障')
    op.drop_column('channel_deliveries', 'protocol_file_id')
    op.drop_column('channel_deliveries', 'file_info')
