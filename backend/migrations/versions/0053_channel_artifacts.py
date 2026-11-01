"""Source-bound, confirmed QQ image and file replies."""
from alembic import op
import sqlalchemy as sa

revision = '0053'
down_revision = '0052'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('channel_artifact_replies',
        sa.Column('id',sa.String(64),primary_key=True),
        sa.Column('workspace_id',sa.String(64),nullable=False),
        sa.Column('created_at',sa.Float,nullable=False),
        sa.Column('user_id',sa.String(64),nullable=False),
        sa.Column('channel_id',sa.String(64),nullable=False),
        sa.Column('origin_id',sa.String(64),nullable=False),
        sa.Column('login_id',sa.String(64),nullable=False),
        sa.Column('request_id',sa.String(100),nullable=False),
        sa.Column('request_hash',sa.String(64),nullable=False),
        sa.Column('plan',sa.JSON,nullable=False),
        sa.Column('digest',sa.String(64),nullable=False),
        sa.Column('status',sa.String(30),nullable=False),
        sa.Column('expires_at',sa.Float,nullable=False),
        sa.Column('inbound_id',sa.String(64),nullable=True),
        sa.UniqueConstraint('user_id','request_id'))
    for key in ('workspace_id','user_id','channel_id','origin_id'):
        op.create_index('ix_channel_artifact_replies_'+key,'channel_artifact_replies',[key])


def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM channel_artifact_replies')).scalar():
        raise RuntimeError('已有 QQ 附件确认与去重记录，请使用升级前备份和配套镜像回退')
    op.drop_table('channel_artifact_replies')
