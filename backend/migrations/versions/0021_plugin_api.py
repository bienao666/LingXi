"""API plugin connections; request confirmation and send markers use invocation JSON."""
from alembic import op
import sqlalchemy as sa

revision = '0021'
down_revision = '0020'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('plugin_connections',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('installation_id', sa.String(64), nullable=False),
        sa.Column('capability_id', sa.String(120), nullable=False),
        sa.Column('binding_digest', sa.String(64), nullable=False),
        sa.Column('encrypted_secret', sa.Text(), nullable=False),
        sa.Column('allow_private', sa.Boolean(), nullable=False),
        sa.Column('status', sa.String(30), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('checked_at', sa.Float(), nullable=True),
        sa.Column('updated_at', sa.Float(), nullable=False),
        sa.UniqueConstraint('installation_id', 'capability_id'))
    for column in ('workspace_id', 'user_id', 'installation_id'):
        op.create_index('ix_plugin_connections_'+column, 'plugin_connections', [column])


def downgrade():
    releases = sa.text("SELECT count(*) FROM plugin_releases WHERE manifest" +
        ("::text" if op.get_bind().dialect.name == 'postgresql' else '') + " LIKE '%\"api\"%'")
    if op.get_bind().scalar(sa.text('SELECT count(*) FROM plugin_connections')) or op.get_bind().scalar(
            sa.text("SELECT count(*) FROM plugin_invocations WHERE config::text LIKE '%\"api\"%'")
            if op.get_bind().dialect.name == 'postgresql' else
            sa.text("SELECT count(*) FROM plugin_invocations WHERE config LIKE '%\"api\"%'")) or op.get_bind().scalar(releases):
        raise RuntimeError('仍有 API 授权或调用回执；请恢复升级前备份及配套镜像，不可直接降级遗失副作用状态')
    op.drop_table('plugin_connections')
