"""Immutable bundled releases and owner-scoped plugin lifecycle."""
from alembic import op
import sqlalchemy as sa
revision = '0011'
down_revision = '0010'


def entity(name, *columns, indexes=(), unique=()):
    op.create_table(name, sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('user_id', sa.String(64), nullable=False), *columns,
        *[sa.UniqueConstraint(*keys) for keys in unique])
    for field in ('workspace_id', 'user_id', *indexes):
        op.create_index(f'ix_{name}_{field}', name, [field])


def col(name, type_, nullable=False):
    return sa.Column(name, type_, nullable=nullable)


def upgrade():
    op.create_table('plugin_releases', col('id', sa.String(64)), col('plugin_id', sa.String(120)),
        col('version', sa.String(30)), col('digest', sa.String(64)), col('manifest', sa.JSON()),
        col('source', sa.String(30)), col('status', sa.String(30)),
        sa.PrimaryKeyConstraint('id'), sa.UniqueConstraint('plugin_id', 'version'))
    op.create_index('ix_plugin_releases_plugin_id', 'plugin_releases', ['plugin_id'])
    entity('plugin_installations', col('plugin_id', sa.String(120)), col('release_id', sa.String(64)),
        col('version', sa.Integer()), col('status', sa.String(30)), col('grants', sa.JSON()),
        col('config', sa.JSON()), col('dependency_lock', sa.JSON()), col('updated_at', sa.Float()),
        unique=(('user_id', 'workspace_id', 'plugin_id'),))
    entity('plugin_snapshots', col('installation_id', sa.String(64)), col('number', sa.Integer()),
        col('release_id', sa.String(64)), col('grants', sa.JSON()), col('config', sa.JSON()),
        col('dependency_lock', sa.JSON()), indexes=('installation_id',), unique=(('installation_id', 'number'),))
    entity('plugin_previews', col('login_session_id', sa.String(64)), col('plan', sa.JSON()),
        col('digest', sa.String(64)), col('expires_at', sa.Float()), col('status', sa.String(30)), col('result', sa.JSON()))
    entity('plugin_invocations', col('request_id', sa.String(100)), col('request_hash', sa.String(64)),
        col('installation_id', sa.String(64)), col('release_id', sa.String(64)), col('capability_id', sa.String(120)),
        col('input', sa.Text()), col('config', sa.JSON()), col('result', sa.Text()), col('receipt', sa.JSON()),
        col('status', sa.String(30)), col('version', sa.Integer()), col('error', sa.Text(), True),
        col('finished_at', sa.Float(), True), indexes=('installation_id', 'status'),
        unique=(('user_id', 'workspace_id', 'request_id'),))


def downgrade():
    for name in ('plugin_invocations', 'plugin_previews', 'plugin_snapshots', 'plugin_installations', 'plugin_releases'):
        op.drop_table(name)
