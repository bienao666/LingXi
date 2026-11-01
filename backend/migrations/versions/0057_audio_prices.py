"""Owner supplied audio unit prices; historical measurements remain immutable."""
from alembic import op
import sqlalchemy as sa

revision = '0057'
down_revision = '0056'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('audio_prices',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('provider_id', sa.String(64), nullable=False),
        sa.Column('model', sa.String(150), nullable=False),
        sa.Column('kind', sa.String(30), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('config', sa.JSON(), nullable=False))
    for name in ('workspace_id', 'user_id'):
        op.create_index('ix_audio_prices_' + name, 'audio_prices', [name])


def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM audio_prices')).scalar():
        raise RuntimeError('已有语音单价，请使用升级前备份与配套镜像整体回退')
    op.drop_table('audio_prices')
