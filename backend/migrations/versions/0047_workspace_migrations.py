"""Durable server handover and source write fences."""
from alembic import op
import sqlalchemy as sa

revision = '0047'
down_revision = '0046'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('migration_sites',
        sa.Column('id', sa.String(20), primary_key=True),
        sa.Column('instance_id', sa.String(32), nullable=False),
        sa.Column('mode', sa.String(30), nullable=False),
        sa.Column('origin', sa.String(500), nullable=False),
        sa.Column('migration_id', sa.String(32), nullable=True),
        sa.Column('ticket', sa.Text(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False))
    op.create_table('workspace_migrations',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('workspace_id', sa.String(64), nullable=False, index=True),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('user_id', sa.String(64), nullable=False, index=True),
        sa.Column('request_id', sa.String(100), nullable=False),
        sa.Column('request_digest', sa.String(64), nullable=False),
        sa.Column('direction', sa.String(10), nullable=False),
        sa.Column('status', sa.String(30), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('source_id', sa.String(32), nullable=False),
        sa.Column('target_id', sa.String(32), nullable=False),
        sa.Column('target_label', sa.String(80), nullable=False),
        sa.Column('target_origin', sa.String(500), nullable=False),
        sa.Column('target_ticket', sa.Text(), nullable=False),
        sa.Column('manifest_digest', sa.String(64), nullable=False),
        sa.Column('finished_at', sa.Float(), nullable=False),
        sa.UniqueConstraint('user_id', 'workspace_id', 'request_id'))


def downgrade():
    if (op.get_bind().scalar(sa.text('SELECT count(*) FROM migration_sites'))
            or op.get_bind().scalar(sa.text('SELECT count(*) FROM workspace_migrations'))):
        raise RuntimeError('已有迁移身份或停写屏障，不能有损降级；请先核对迁移与配套备份')
    op.drop_table('workspace_migrations')
    op.drop_table('migration_sites')
