"""Explicit native provider protocols without reinterpreting existing credentials."""
from alembic import op
import sqlalchemy as sa
revision = '0016'
down_revision = '0015'


def upgrade():
    op.create_table('provider_protocols',
        sa.Column('provider_id', sa.String(64), primary_key=True),
        sa.Column('protocol', sa.String(20), nullable=False))


def downgrade():
    if op.get_bind().execute(sa.text("SELECT count(*) FROM provider_protocols WHERE protocol <> 'openai'")).scalar():
        raise RuntimeError('Native connections cannot be reinterpreted as OpenAI; explicitly migrate them before downgrade')
    op.drop_table('provider_protocols')
