"""Encrypted TOTP enrollment and hashed one-use recovery codes."""
from alembic import op
import sqlalchemy as sa

revision = '0014'
down_revision = '0013'


def upgrade():
    op.create_table('two_factors',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('workspace_id', sa.String(64), nullable=False, index=True),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('user_id', sa.String(64), nullable=False, index=True),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('encrypted_secret', sa.Text(), nullable=False),
        sa.Column('last_counter', sa.Integer(), nullable=False),
        sa.Column('recovery_hashes', sa.JSON(), nullable=False),
        sa.Column('pending_secret', sa.Text(), nullable=False),
        sa.Column('pending_session', sa.String(64), nullable=False),
        sa.Column('pending_request', sa.String(100), nullable=False),
        sa.Column('pending_expires', sa.Float(), nullable=False),
        sa.UniqueConstraint('user_id', 'workspace_id'))


def downgrade():
    # A binary/database rollback must never silently bypass an enabled factor.
    bind = op.get_bind()
    if bind.execute(sa.text('SELECT count(*) FROM two_factors WHERE enabled = true')).scalar():
        raise RuntimeError('Disable TOTP with verified owner proof before downgrading; enabled factors cannot be discarded.')
    op.drop_table('two_factors')
