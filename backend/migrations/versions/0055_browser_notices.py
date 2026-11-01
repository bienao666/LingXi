"""Opt-in browser notification consent and durable per-contact attempt barriers."""
from alembic import op
import sqlalchemy as sa

revision='0055'
down_revision='0054'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('browser_notice_preferences',
        sa.Column('user_id',sa.String(64),primary_key=True),
        sa.Column('workspace_id',sa.String(64),nullable=False),
        sa.Column('enabled',sa.Boolean(),nullable=False),
        sa.Column('version',sa.Integer(),nullable=False))
    op.create_table('browser_notices',
        sa.Column('id',sa.String(64),primary_key=True),
        sa.Column('workspace_id',sa.String(64),nullable=False),
        sa.Column('created_at',sa.Float(),nullable=False),
        sa.Column('user_id',sa.String(64),nullable=False),
        sa.Column('contact_id',sa.String(64),nullable=False),
        sa.Column('preference_version',sa.Integer(),nullable=False),
        sa.Column('consent_version',sa.Integer(),nullable=False),
        sa.Column('expires_at',sa.Float(),nullable=False),
        sa.Column('status',sa.String(30),nullable=False),
        sa.Column('login_id',sa.String(64),nullable=False),
        sa.Column('attempted_at',sa.Float(),nullable=True),
        sa.UniqueConstraint('contact_id'))
    for key in ('workspace_id','user_id'):
        op.create_index('ix_browser_notices_'+key,'browser_notices',[key])


def downgrade():
    db=op.get_bind()
    if db.execute(sa.text('SELECT count(*) FROM browser_notices')).scalar() or db.execute(sa.text('SELECT count(*) FROM browser_notice_preferences')).scalar():
        raise RuntimeError('已有通知同意或尝试屏障，请用升级前备份和配套镜像回退')
    op.drop_table('browser_notices')
    op.drop_table('browser_notice_preferences')
