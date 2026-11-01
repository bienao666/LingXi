"""Per-request QQ execution approval and durable result routing."""
from alembic import op
import sqlalchemy as sa

revision = '0037'
down_revision = '0036'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('channel_tasks',
        sa.Column('id',sa.String(64),primary_key=True),
        sa.Column('workspace_id',sa.String(64),nullable=False),
        sa.Column('created_at',sa.Float(),nullable=False),
        sa.Column('user_id',sa.String(64),nullable=False),
        sa.Column('channel_id',sa.String(64),nullable=False),
        sa.Column('inbound_id',sa.String(64),nullable=False,unique=True),
        sa.Column('actor_id',sa.String(64),nullable=False),
        sa.Column('conversation_id',sa.String(64),nullable=False),
        sa.Column('source_id',sa.String(64),nullable=False),
        sa.Column('source_hash',sa.String(64),nullable=False),
        sa.Column('goal',sa.Text(),nullable=False),
        sa.Column('scope',sa.JSON(),nullable=False),
        sa.Column('media_refs',sa.JSON(),nullable=False),
        sa.Column('status',sa.String(30),nullable=False),
        sa.Column('version',sa.Integer(),nullable=False),
        sa.Column('preview',sa.JSON(),nullable=False),
        sa.Column('run_id',sa.String(64),nullable=True,unique=True),
        sa.Column('notifications',sa.JSON(),nullable=False),
        sa.Column('expires_at',sa.Float(),nullable=False),
        sa.Column('error',sa.Text(),nullable=True))
    for column in ('workspace_id','user_id','channel_id','conversation_id','source_id','status','expires_at'):
        op.create_index('ix_channel_tasks_'+column,'channel_tasks',[column])


def downgrade():
    if op.get_bind().scalar(sa.text('SELECT count(*) FROM channel_tasks')):
        raise RuntimeError('已有 QQ 授权或投递记录，请恢复升级前备份和配套镜像，不能丢弃失效屏障')
    op.drop_table('channel_tasks')
