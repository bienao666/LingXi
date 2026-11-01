"""Opt-in short-lived emotion observations and non-replayable analysis jobs."""
from alembic import op
import sqlalchemy as sa
import json

revision = '0036'
down_revision = '0035'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('emotion_observations',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('conversation_id', sa.String(64), nullable=False),
        sa.Column('source_id', sa.String(64), nullable=False),
        sa.Column('source_hash', sa.String(64), nullable=False),
        sa.Column('preference_version', sa.Integer(), nullable=False),
        sa.Column('model_id', sa.String(64), nullable=False),
        sa.Column('config', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('label', sa.String(24), nullable=False),
        sa.Column('evidence', sa.String(24), nullable=False),
        sa.Column('quote', sa.String(300), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('expires_at', sa.Float(), nullable=False),
        sa.Column('error', sa.String(200), nullable=True))
    for column in ('workspace_id', 'user_id', 'conversation_id', 'status', 'expires_at', 'source_id'):
        op.create_index('ix_emotion_observations_' + column, 'emotion_observations', [column], unique=column == 'source_id')


def downgrade():
    if op.get_bind().scalar(sa.text('SELECT count(*) FROM emotion_observations')):
        raise RuntimeError('已有情绪来源及反馈记录，请恢复升级前备份和镜像，不能丢弃失效屏障')
    for spec in op.get_bind().execute(sa.text('SELECT spec FROM support_preferences')).scalars():
        if isinstance(spec, str):
            spec = json.loads(spec)
        if spec.get('auto_observe'):
            raise RuntimeError('请先关闭自动情绪线索并保存，再恢复配套旧版本')
    op.drop_table('emotion_observations')
