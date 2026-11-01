"""Short lived, single use native transcription authorizations; no audio storage."""
from alembic import op
import sqlalchemy as sa

revision = '0050'
down_revision = '0049'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('voice_realtime_tickets',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('login_session_id', sa.String(64), nullable=False),
        sa.Column('voice_session_id', sa.String(64), nullable=False),
        sa.Column('config_version', sa.Integer, nullable=False),
        sa.Column('provider_version', sa.Integer, nullable=False),
        sa.Column('validation', sa.Boolean, nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('created_at', sa.Float, nullable=False),
        sa.Column('expires_at', sa.Float, nullable=False))
    op.create_index('ix_voice_realtime_tickets_user_id', 'voice_realtime_tickets', ['user_id'])


def downgrade():
    op.drop_table('voice_realtime_tickets')
