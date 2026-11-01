"""QQ speech reply job fence and encrypted transient waveform."""
from alembic import op
import sqlalchemy as sa

revision = '0030'
down_revision = '0029'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('channel_deliveries', sa.Column('kind', sa.String(20), nullable=False, server_default='text'))
    op.add_column('channel_deliveries', sa.Column('speech', sa.JSON(), nullable=False, server_default='{}'))
    op.add_column('channel_deliveries', sa.Column('encrypted_audio', sa.Text(), nullable=True))
    op.alter_column('channel_deliveries', 'kind', server_default=None)
    op.alter_column('channel_deliveries', 'speech', server_default=None)


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM channel_deliveries WHERE kind = 'record'")):
        raise RuntimeError('已有 QQ 语音回复记录；请恢复升级前备份和镜像，不能丢弃付费调用及投递屏障')
    for name in ('encrypted_audio', 'speech', 'kind'):
        op.drop_column('channel_deliveries', name)
