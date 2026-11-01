"""Independent store tables; no Core tables or references."""
from alembic import op
import sqlalchemy as sa
revision='store0001'
down_revision=None


def upgrade():
    # Explicit schema: future ORM additions cannot silently enter this migration.
    op.create_table('store_accounts',sa.Column('id',sa.String(64),primary_key=True),sa.Column('username',sa.String(100),nullable=False,unique=True),sa.Column('password_hash',sa.Text(),nullable=False),sa.Column('role',sa.String(20),nullable=False),sa.Column('namespace',sa.String(60),unique=True),sa.Column('audiences',sa.JSON(),nullable=False),sa.Column('enabled',sa.Boolean(),nullable=False))
    op.create_table('store_logins',sa.Column('id',sa.String(64),primary_key=True),sa.Column('account_id',sa.String(64),nullable=False,index=True),sa.Column('token_hash',sa.String(64),nullable=False,unique=True),sa.Column('csrf',sa.String(100),nullable=False),sa.Column('expires_at',sa.Float(),nullable=False))
    op.create_table('store_readers',sa.Column('id',sa.String(64),primary_key=True),sa.Column('token_hash',sa.String(64),nullable=False,unique=True),sa.Column('audience',sa.String(100),nullable=False),sa.Column('expires_at',sa.Float(),nullable=False),sa.Column('enabled',sa.Boolean(),nullable=False))
    op.create_table('store_releases',sa.Column('id',sa.String(64),primary_key=True),sa.Column('plugin_id',sa.String(120),nullable=False,index=True),sa.Column('version',sa.String(30),nullable=False),sa.Column('publisher_id',sa.String(64),nullable=False,index=True),sa.Column('visibility',sa.String(20),nullable=False),sa.Column('audience',sa.String(100),nullable=False),sa.Column('manifest',sa.JSON(),nullable=False),sa.Column('digest',sa.String(64),nullable=False),sa.Column('status',sa.String(20),nullable=False,index=True),sa.Column('version_number',sa.Integer(),nullable=False),sa.Column('checks',sa.JSON(),nullable=False),sa.Column('review_note',sa.String(1000),nullable=False),sa.Column('reviewer_id',sa.String(64)),sa.Column('artifact',sa.JSON(),nullable=False),sa.Column('submitted_at',sa.Float(),nullable=False),sa.Column('published_at',sa.Float()),sa.UniqueConstraint('plugin_id','version'))
    op.create_table('store_state',sa.Column('id',sa.String(20),primary_key=True),sa.Column('revision',sa.Integer(),nullable=False))
    op.bulk_insert(sa.table('store_state',sa.column('id',sa.String()),sa.column('revision',sa.Integer())),[{'id':'catalog','revision':0}])
    op.create_table('store_audits',sa.Column('id',sa.String(64),primary_key=True),sa.Column('account_id',sa.String(64),nullable=False),sa.Column('release_id',sa.String(64),nullable=False),sa.Column('action',sa.String(40),nullable=False),sa.Column('note',sa.String(1000),nullable=False),sa.Column('created_at',sa.Float(),nullable=False))
    op.create_table('store_login_attempts',sa.Column('id',sa.String(64),primary_key=True),sa.Column('failures',sa.Integer(),nullable=False),sa.Column('window',sa.Float(),nullable=False))


def downgrade():
    for name in ('store_login_attempts','store_audits','store_state','store_releases','store_readers','store_logins','store_accounts'):op.drop_table(name)
