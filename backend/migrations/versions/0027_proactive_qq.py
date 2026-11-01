"""Explicit private QQ proactive routing and durable send barriers."""
from alembic import op
import sqlalchemy as sa

revision='0027'
down_revision='0026'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('contact_routes',
        sa.Column('user_id',sa.String(64),primary_key=True),
        sa.Column('workspace_id',sa.String(64),nullable=False),
        sa.Column('target',sa.JSON(),nullable=False))
    op.create_table('proactive_deliveries',
        sa.Column('id',sa.String(64),primary_key=True),
        sa.Column('workspace_id',sa.String(64),nullable=False),
        sa.Column('created_at',sa.Float(),nullable=False),
        sa.Column('user_id',sa.String(64),nullable=False),
        sa.Column('contact_id',sa.String(64),nullable=False,unique=True),
        sa.Column('topic_id',sa.String(64),nullable=False,unique=True),
        sa.Column('kind',sa.String(10),nullable=False),
        sa.Column('channel_id',sa.String(64),nullable=True),
        sa.Column('target',sa.JSON(),nullable=False),
        sa.Column('status',sa.String(30),nullable=False),
        sa.Column('echo',sa.String(100),nullable=True,unique=True),
        sa.Column('sent_at',sa.Float(),nullable=True),
        sa.Column('protocol_message_id',sa.String(60),nullable=True),
        sa.Column('error',sa.Text(),nullable=True),
        sa.Column('reviewed_at',sa.Float(),nullable=True))
    for name in ('workspace_id','user_id','channel_id'):
        op.create_index('ix_proactive_deliveries_'+name,'proactive_deliveries',[name])


def downgrade():
    for name in ('contact_routes','proactive_deliveries'):
        if op.get_bind().scalar(sa.text('SELECT count(*) FROM '+name)):
            raise RuntimeError('已有主动投递授权或回执；请使用升级前备份与配套镜像，不能丢弃发送屏障')
    op.drop_table('proactive_deliveries')
    op.drop_table('contact_routes')
