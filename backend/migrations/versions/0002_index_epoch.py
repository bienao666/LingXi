"""Separate embedding index generations; never mix vectors after a rebuild."""
from alembic import op
import sqlalchemy as sa

revision = '0002'
down_revision = '0001'


def upgrade():
    op.add_column('memories', sa.Column('index_epoch', sa.Integer(), nullable=False, server_default='0'))


def downgrade():
    op.drop_column('memories', 'index_epoch')
