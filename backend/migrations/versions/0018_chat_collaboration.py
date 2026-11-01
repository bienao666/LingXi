"""Protect chat-linked delegation from rollback to a core without origin gates."""
from alembic import op
import sqlalchemy as sa

revision = '0018'
down_revision = '0017'
branch_labels = None
depends_on = None


def upgrade():
    pass  # Reuses the durable chat_actions and agent_runs tables.


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM chat_actions WHERE capability = 'agent.delegate'")):
        raise RuntimeError('聊天协作关联仍存在；请恢复完整升级前备份及配套镜像，不可降级绕过来源屏障')
