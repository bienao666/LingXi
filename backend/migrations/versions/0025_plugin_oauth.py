"""OAuth PKCE attempts, encrypted grants and durable refresh boundaries."""
from alembic import op
import sqlalchemy as sa

revision = '0025'
down_revision = '0024'
branch_labels = None
depends_on = None


def upgrade():
    common = lambda: [sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('connection_id', sa.String(64), nullable=False),
        sa.Column('encrypted_config', sa.Text(), nullable=False),
        sa.Column('status', sa.String(30), nullable=False),
        sa.Column('expires_at', sa.Float(), nullable=False)]
    op.create_table('plugin_oauth_grants', *common(),
        sa.Column('scopes', sa.JSON(), nullable=False), sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('operation', sa.String(64), nullable=False), sa.Column('deadline', sa.Float(), nullable=False),
        sa.Column('remote_status', sa.String(30), nullable=False), sa.UniqueConstraint('connection_id'))
    op.create_table('plugin_oauth_flows', *common(),
        sa.Column('login_id', sa.String(64), nullable=False), sa.Column('state_hash', sa.String(64), nullable=False),
        sa.Column('snapshot', sa.JSON(), nullable=False), sa.UniqueConstraint('state_hash'))
    for table in ('plugin_oauth_grants', 'plugin_oauth_flows'):
        for field in ('workspace_id', 'user_id', 'connection_id'):
            op.create_index('ix_'+table+'_'+field, table, [field])


def downgrade():
    cast = '::text' if op.get_bind().dialect.name == 'postgresql' else ''
    if (op.get_bind().scalar(sa.text('SELECT count(*) FROM plugin_oauth_grants')) or
            op.get_bind().scalar(sa.text('SELECT count(*) FROM plugin_oauth_flows')) or
            op.get_bind().scalar(sa.text(f"SELECT count(*) FROM plugin_releases WHERE manifest{cast} LIKE '%\"oauth2\"%'"))):
        raise RuntimeError('仍有 OAuth 版本、授权或发送屏障；请使用升级前备份及配套镜像，不能直接降级')
    op.drop_table('plugin_oauth_flows')
    op.drop_table('plugin_oauth_grants')
