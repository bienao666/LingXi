"""Persist encrypted application proxy policy independently of unfinished features."""
from alembic import op
import sqlalchemy as sa

revision = '0003'
down_revision = '0002'


def upgrade():
    op.create_table('system_proxy',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('encrypted_config', sa.Text(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('updated_at', sa.Float(), nullable=False))


def downgrade():
    op.drop_table('system_proxy')
