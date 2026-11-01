"""Index complete model history without changing stored calls or prices."""
from alembic import op

revision = '0049'
down_revision = '0048'
branch_labels = None
depends_on = None


def upgrade():
    op.create_index('ix_model_calls_page', 'model_calls', ['workspace_id', 'created_at', 'id'])


def downgrade():
    op.drop_index('ix_model_calls_page', table_name='model_calls')
