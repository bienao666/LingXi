"""Durable skill tasks, attempts, events and one-shot reminder receipts."""
from alembic import op
import sqlalchemy as sa

revision = '0005'
down_revision = '0004'


def entity():
    return [sa.Column('id', sa.String(64), primary_key=True),
            sa.Column('workspace_id', sa.String(64), nullable=False),
            sa.Column('created_at', sa.Float(), nullable=False)]


def indexes(table, *columns):
    for column in ('workspace_id', *columns):
        op.create_index('ix_'+table+'_'+column, table, [column])


def upgrade():
    op.create_table('work_tasks', *entity(),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('request_id', sa.String(100), nullable=False),
        sa.Column('request_hash', sa.String(64), nullable=False),
        sa.Column('title', sa.String(200), nullable=False),
        sa.Column('skill_id', sa.String(100), nullable=False),
        sa.Column('skill_version', sa.String(30), nullable=False),
        sa.Column('input', sa.JSON(), nullable=False),
        sa.Column('config', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(30), nullable=False),
        sa.Column('cancel_requested', sa.Boolean(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('next_seq', sa.Integer(), nullable=False),
        sa.Column('conversation_id', sa.String(64), nullable=True),
        sa.Column('result_message_id', sa.String(64), nullable=True),
        sa.Column('result', sa.JSON(), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('updated_at', sa.Float(), nullable=False),
        sa.UniqueConstraint('user_id', 'request_id'))
    indexes('work_tasks', 'user_id', 'status')
    op.create_table('task_attempts', *entity(),
        sa.Column('task_id', sa.String(64), nullable=False),
        sa.Column('number', sa.Integer(), nullable=False),
        sa.Column('lease_token', sa.String(64), nullable=False),
        sa.Column('status', sa.String(30), nullable=False),
        sa.Column('finished_at', sa.Float(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('receipt', sa.JSON(), nullable=False),
        sa.UniqueConstraint('task_id', 'number'))
    indexes('task_attempts', 'task_id')
    op.create_table('task_events',
        sa.Column('task_id', sa.String(64), primary_key=True),
        sa.Column('seq', sa.Integer(), primary_key=True),
        sa.Column('kind', sa.String(30), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('data', sa.JSON(), nullable=False))
    op.create_table('reminders', *entity(),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('request_id', sa.String(100), nullable=False),
        sa.Column('request_hash', sa.String(64), nullable=False),
        sa.Column('title', sa.String(200), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('due_at', sa.Float(), nullable=False),
        sa.Column('timezone', sa.String(100), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(30), nullable=False),
        sa.Column('conversation_id', sa.String(64), nullable=True),
        sa.Column('updated_at', sa.Float(), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.UniqueConstraint('user_id', 'request_id'))
    indexes('reminders', 'user_id', 'status', 'due_at')
    op.create_table('reminder_receipts', *entity(),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('reminder_id', sa.String(64), nullable=False),
        sa.Column('reminder_version', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(30), nullable=False),
        sa.Column('message_id', sa.String(64), nullable=True),
        sa.Column('acknowledged_at', sa.Float(), nullable=True),
        sa.UniqueConstraint('reminder_id', 'reminder_version'))
    indexes('reminder_receipts', 'user_id', 'reminder_id')


def downgrade():
    for table in ('reminder_receipts','reminders','task_events','task_attempts','work_tasks'):
        op.drop_table(table)
