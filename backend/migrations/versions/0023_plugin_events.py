"""Signed plugin event subscriptions and immutable dedupe receipts."""
from alembic import op
import sqlalchemy as sa

revision = '0023'
down_revision = '0022'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('plugin_event_subscriptions',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('installation_id', sa.String(64), nullable=False),
        sa.Column('capability_id', sa.String(120), nullable=False),
        sa.Column('release_id', sa.String(64), nullable=False),
        sa.Column('installation_version', sa.Integer(), nullable=False),
        sa.Column('binding_digest', sa.String(64), nullable=False),
        sa.Column('dependency_lock', sa.JSON(), nullable=False),
        sa.Column('encrypted_secret', sa.Text(), nullable=False),
        sa.Column('event_types', sa.JSON(), nullable=False),
        sa.Column('hourly_limit', sa.Integer(), nullable=False),
        sa.Column('daily_limit', sa.Integer(), nullable=False),
        sa.Column('sequence', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(30), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('updated_at', sa.Float(), nullable=False),
        sa.UniqueConstraint('installation_id', 'capability_id'))
    op.create_table('plugin_events',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('subscription_id', sa.String(64), nullable=False),
        sa.Column('installation_id', sa.String(64), nullable=False),
        sa.Column('release_id', sa.String(64), nullable=False),
        sa.Column('subscription_version', sa.Integer(), nullable=False),
        sa.Column('external_id', sa.String(100), nullable=False),
        sa.Column('body_digest', sa.String(64), nullable=False),
        sa.Column('sequence', sa.Integer(), nullable=False),
        sa.Column('event_type', sa.String(120), nullable=False),
        sa.Column('title', sa.String(200), nullable=False),
        sa.Column('summary', sa.Text(), nullable=False),
        sa.Column('occurred_at', sa.Float(), nullable=False),
        sa.Column('expires_at', sa.Float(), nullable=False),
        sa.Column('status', sa.String(30), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.UniqueConstraint('subscription_id', 'external_id'),
        sa.UniqueConstraint('subscription_id', 'sequence'))
    for table, columns in [('plugin_event_subscriptions', ('workspace_id', 'user_id', 'installation_id')),
                           ('plugin_events', ('workspace_id', 'user_id', 'subscription_id', 'installation_id', 'expires_at'))]:
        for col in columns:
            op.create_index('ix_' + table + '_' + col, table, [col])


def downgrade():
    cast = '::text' if op.get_bind().dialect.name == 'postgresql' else ''
    if (op.get_bind().scalar(sa.text('SELECT count(*) FROM plugin_event_subscriptions')) or
            op.get_bind().scalar(sa.text('SELECT count(*) FROM plugin_events')) or
            op.get_bind().scalar(sa.text(f"SELECT count(*) FROM plugin_releases WHERE manifest{cast} LIKE '%\"event\"%'"))):
        raise RuntimeError('仍有事件订阅、去重或删除记录；使用升级前备份及配套镜像，不能直接降级遗失授权与回放屏障')
    op.drop_table('plugin_events')
    op.drop_table('plugin_event_subscriptions')
