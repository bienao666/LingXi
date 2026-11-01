"""Durable QQ audio conversion and ASR call fence."""
from alembic import op
import sqlalchemy as sa

revision = '0029'
down_revision = '0028'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('channel_attachments', sa.Column('audio_state', sa.JSON(), nullable=False, server_default='{}'))
    op.alter_column('channel_attachments', 'audio_state', server_default=None)


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM channel_attachments WHERE kind = 'record'")):
        raise RuntimeError('已有 QQ 语音记录；请恢复升级前备份和镜像，不能丢弃识别调用屏障')
    op.drop_column('channel_attachments', 'audio_state')
