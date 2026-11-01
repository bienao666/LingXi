"""Scoped remote catalog trust, credentials and access state."""
from alembic import op
import sqlalchemy as sa
revision='0012'
down_revision='0011'


def upgrade():
    common=lambda:[sa.Column('id',sa.String(64),primary_key=True),sa.Column('workspace_id',sa.String(64),nullable=False,index=True),sa.Column('created_at',sa.Float(),nullable=False),sa.Column('user_id',sa.String(64),nullable=False,index=True)]
    op.create_table('plugin_store_connections',*common(),sa.Column('url',sa.String(1000),nullable=False),sa.Column('enabled',sa.Boolean(),nullable=False),sa.Column('allow_private',sa.Boolean(),nullable=False),sa.Column('root_keys',sa.JSON(),nullable=False),sa.Column('audience',sa.String(100),nullable=False),sa.Column('encrypted_token',sa.Text(),nullable=False),sa.Column('version',sa.Integer(),nullable=False),sa.Column('revision',sa.Integer(),nullable=False),sa.Column('trust_epoch',sa.Integer(),nullable=False),sa.Column('checked_at',sa.Float(),nullable=False),sa.Column('expires_at',sa.Float(),nullable=False),sa.Column('error',sa.String(500)),sa.UniqueConstraint('user_id','workspace_id'))
    op.create_table('plugin_store_access',*common(),sa.Column('connection_id',sa.String(64),nullable=False,index=True),sa.Column('release_id',sa.String(64),nullable=False,index=True),sa.Column('status',sa.String(30),nullable=False),sa.Column('key_id',sa.String(64),nullable=False),sa.Column('signature',sa.JSON(),nullable=False),sa.UniqueConstraint('user_id','workspace_id','release_id'))


def downgrade():
    op.drop_table('plugin_store_access');op.drop_table('plugin_store_connections')
