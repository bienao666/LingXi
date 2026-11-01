"""Remote MCP discovery snapshots and grants; send fences remain in invocations."""
from alembic import op
import sqlalchemy as sa

revision = '0022'
down_revision = '0021'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('plugin_mcp_catalogs',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('connection_id', sa.String(64), nullable=False),
        sa.Column('connection_version', sa.Integer(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('tools', sa.JSON(), nullable=False),
        sa.Column('digest', sa.String(64), nullable=False),
        sa.Column('approved_tools', sa.JSON(), nullable=False),
        sa.Column('approved_at', sa.Float(), nullable=True),
        sa.Column('discovered_at', sa.Float(), nullable=True),
        sa.UniqueConstraint('connection_id'))
    for col in ('workspace_id', 'user_id', 'connection_id'):
        op.create_index('ix_plugin_mcp_catalogs_'+col, 'plugin_mcp_catalogs', [col])


def downgrade():
    cast = '::text' if op.get_bind().dialect.name == 'postgresql' else ''
    if (op.get_bind().scalar(sa.text('SELECT count(*) FROM plugin_mcp_catalogs')) or
        op.get_bind().scalar(sa.text(f"SELECT count(*) FROM plugin_invocations WHERE config{cast} LIKE '%\"mcp\"%'")) or
        op.get_bind().scalar(sa.text(f"SELECT count(*) FROM plugin_releases WHERE manifest{cast} LIKE '%\"mcp\"%'"))):
        raise RuntimeError('仍有 MCP 目录、授权或调用回执；请使用升级前备份及配套镜像，不能直接降级遗失副作用状态')
    op.drop_table('plugin_mcp_catalogs')
