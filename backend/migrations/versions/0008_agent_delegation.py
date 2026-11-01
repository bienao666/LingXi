"""Bounded delegation, context references and durable graph events."""
from alembic import op
import sqlalchemy as sa
revision = '0008'
down_revision = '0007'

def upgrade():
    op.create_table('agent_runs',
        sa.Column('user_id', sa.String(length=64), nullable=False, primary_key=False),
        sa.Column('request_id', sa.String(length=100), nullable=False, primary_key=False),
        sa.Column('request_hash', sa.String(length=64), nullable=False, primary_key=False),
        sa.Column('title', sa.String(length=200), nullable=False, primary_key=False),
        sa.Column('goal', sa.Text(), nullable=False, primary_key=False),
        sa.Column('status', sa.String(length=30), nullable=False, primary_key=False),
        sa.Column('version', sa.Integer(), nullable=False, primary_key=False),
        sa.Column('round', sa.Integer(), nullable=False, primary_key=False),
        sa.Column('config', sa.JSON(), nullable=False, primary_key=False),
        sa.Column('decision', sa.JSON(), nullable=False, primary_key=False),
        sa.Column('memory_refs', sa.JSON(), nullable=False, primary_key=False),
        sa.Column('calls_used', sa.Integer(), nullable=False, primary_key=False),
        sa.Column('max_calls', sa.Integer(), nullable=False, primary_key=False),
        sa.Column('cancel_requested', sa.Boolean(), nullable=False, primary_key=False),
        sa.Column('conversation_id', sa.String(length=64), nullable=True, primary_key=False),
        sa.Column('result_message_id', sa.String(length=64), nullable=True, primary_key=False),
        sa.Column('result', sa.JSON(), nullable=False, primary_key=False),
        sa.Column('error', sa.Text(), nullable=True, primary_key=False),
        sa.Column('updated_at', sa.Float(), nullable=False, primary_key=False),
        sa.Column('id', sa.String(length=64), nullable=False, primary_key=True),
        sa.Column('workspace_id', sa.String(length=64), nullable=False, primary_key=False),
        sa.Column('created_at', sa.Float(), nullable=False, primary_key=False),
        sa.UniqueConstraint('user_id', 'request_id'),
    )
    op.create_index('ix_agent_runs_status','agent_runs',['status'])
    op.create_index('ix_agent_runs_user_id','agent_runs',['user_id'])
    op.create_index('ix_agent_runs_workspace_id','agent_runs',['workspace_id'])
    op.create_table('agent_nodes',
        sa.Column('run_id', sa.String(length=64), nullable=False, primary_key=False),
        sa.Column('key', sa.String(length=30), nullable=False, primary_key=False),
        sa.Column('title', sa.String(length=200), nullable=False, primary_key=False),
        sa.Column('instruction', sa.Text(), nullable=False, primary_key=False),
        sa.Column('agent_id', sa.String(length=100), nullable=False, primary_key=False),
        sa.Column('capability', sa.String(length=60), nullable=False, primary_key=False),
        sa.Column('resource_path', sa.String(length=400), nullable=False, primary_key=False),
        sa.Column('depends_on', sa.JSON(), nullable=False, primary_key=False),
        sa.Column('config', sa.JSON(), nullable=False, primary_key=False),
        sa.Column('context', sa.JSON(), nullable=False, primary_key=False),
        sa.Column('status', sa.String(length=30), nullable=False, primary_key=False),
        sa.Column('attempt', sa.Integer(), nullable=False, primary_key=False),
        sa.Column('result', sa.JSON(), nullable=False, primary_key=False),
        sa.Column('error', sa.Text(), nullable=True, primary_key=False),
        sa.Column('updated_at', sa.Float(), nullable=False, primary_key=False),
        sa.Column('id', sa.String(length=64), nullable=False, primary_key=True),
        sa.Column('workspace_id', sa.String(length=64), nullable=False, primary_key=False),
        sa.Column('created_at', sa.Float(), nullable=False, primary_key=False),
        sa.UniqueConstraint('run_id', 'key'),
    )
    op.create_index('ix_agent_nodes_run_id','agent_nodes',['run_id'])
    op.create_index('ix_agent_nodes_workspace_id','agent_nodes',['workspace_id'])
    op.create_table('agent_events',
        sa.Column('run_id', sa.String(length=64), nullable=False, primary_key=False),
        sa.Column('node_id', sa.String(length=64), nullable=True, primary_key=False),
        sa.Column('kind', sa.String(length=40), nullable=False, primary_key=False),
        sa.Column('data', sa.JSON(), nullable=False, primary_key=False),
        sa.Column('id', sa.String(length=64), nullable=False, primary_key=True),
        sa.Column('workspace_id', sa.String(length=64), nullable=False, primary_key=False),
        sa.Column('created_at', sa.Float(), nullable=False, primary_key=False),
    )
    op.create_index('ix_agent_events_run_id','agent_events',['run_id'])
    op.create_index('ix_agent_events_workspace_id','agent_events',['workspace_id'])

def downgrade():
    op.drop_table('agent_events')
    op.drop_table('agent_nodes')
    op.drop_table('agent_runs')
