"""Protect confirmed voice delegation and its source-checked spoken results."""
from alembic import op
import sqlalchemy as sa

revision = '0056'
down_revision = '0055'
branch_labels = None
depends_on = None


def upgrade():
    # Results are resolved by message for playback and heard-history checks.
    op.create_index('ix_agent_runs_result_message_id', 'agent_runs', ['result_message_id'])


def downgrade():
    rows = op.get_bind().execute(sa.text('SELECT config FROM chat_runs'))
    import json
    for (value,) in rows:
        config = json.loads(value) if isinstance(value, str) else value
        if config.get('voice_session_id') and config.get('task_assist', {}).get('mode') == 'collaboration':
            raise RuntimeError('已有语音协作及播放来源，请使用升级前备份和配套镜像整体回退')
    op.drop_index('ix_agent_runs_result_message_id', table_name='agent_runs')
