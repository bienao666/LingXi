"""Scoped role bindings for immutable plugin agent templates."""
from alembic import op
import sqlalchemy as sa

revision = '0024'
down_revision = '0023'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('plugin_agent_bindings',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('installation_id', sa.String(64), nullable=False),
        sa.Column('capability_id', sa.String(120), nullable=False),
        sa.Column('release_id', sa.String(64), nullable=False),
        sa.Column('persona_id', sa.String(64), nullable=False),
        sa.Column('model_id', sa.String(64), nullable=True),
        sa.Column('tools', sa.JSON(), nullable=False),
        sa.Column('context_categories', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(30), nullable=False),
        sa.Column('epoch', sa.Integer(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('updated_at', sa.Float(), nullable=False),
        sa.UniqueConstraint('installation_id', 'capability_id'))
    for col in ('workspace_id', 'user_id', 'installation_id'):
        op.create_index('ix_plugin_agent_bindings_'+col, 'plugin_agent_bindings', [col])


def downgrade():
    cast = '::text' if op.get_bind().dialect.name == 'postgresql' else ''
    if (op.get_bind().scalar(sa.text('SELECT count(*) FROM plugin_agent_bindings')) or
            op.get_bind().scalar(sa.text(f"SELECT count(*) FROM plugin_releases WHERE manifest{cast} LIKE '%\"agent\"%'"))):
        raise RuntimeError('仍有插件角色版本或授权记录；请使用升级前备份及配套镜像，不能直接降级遗失授权屏障')
    op.drop_table('plugin_agent_bindings')
