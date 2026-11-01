"""Explicit continuous browser voice sessions and automatic-turn provenance."""
from alembic import op
import sqlalchemy as sa

revision = '0032'
down_revision = '0031'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('voice_sessions', sa.Column('mode', sa.String(20), nullable=False, server_default='manual'))
    op.add_column('voice_sessions', sa.Column('automatic_turns', sa.Integer(), nullable=False, server_default='0'))


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM voice_sessions WHERE mode <> 'manual' OR automatic_turns > 0")):
        raise RuntimeError('已有连续语音会话；请恢复升级前备份，不可丢弃转写确认状态和回合记录')
    op.drop_column('voice_sessions', 'automatic_turns')
    op.drop_column('voice_sessions', 'mode')
