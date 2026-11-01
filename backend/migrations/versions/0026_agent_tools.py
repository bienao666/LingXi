"""Protect task-scoped remote tool proposals and durable invocation origins."""
from alembic import op
import sqlalchemy as sa
import json

revision='0026'
down_revision='0025'
branch_labels=None
depends_on=None


def upgrade():
    # Existing JSON columns retain pinned scopes, proposals and invocation links.
    pass


def downgrade():
    for config in op.get_bind().execute(sa.text('SELECT config FROM agent_runs')).scalars():
        if isinstance(config,str): config=json.loads(config)
        if config.get('tools'):
            raise RuntimeError('仍有任务工具范围，请使用升级前备份和配套镜像')
    cast='::text' if op.get_bind().dialect.name=='postgresql' else ''
    for table,field,marker in [('agent_nodes','config','tool_key'),
            ('plugin_invocations','config','agent_origin'),('plugin_releases','manifest','plugin.call')]:
        if op.get_bind().scalar(sa.text(f"SELECT count(*) FROM {table} WHERE {field}{cast} LIKE :marker"),
                              {'marker':'%"'+marker+'"%'}):
            raise RuntimeError('仍有工具编排或关联调用记录，请使用升级前备份和配套镜像，不能直接降级')
