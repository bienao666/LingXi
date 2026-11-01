"""Effective memory configuration and durable Hindsight request fences."""
from alembic import op
import sqlalchemy as sa

revision = '0061'
down_revision = '0060'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('memory_configuration',
        sa.Column('workspace_id', sa.String(64), primary_key=True),
        sa.Column('mode', sa.String(20), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('epoch', sa.Integer(), nullable=False),
        sa.Column('embedding_fingerprint', sa.String(64), nullable=False),
        sa.Column('phase', sa.String(20), nullable=False),
        sa.Column('verification', sa.JSON(), nullable=False),
        sa.Column('pending', sa.JSON(), nullable=False),
        sa.Column('rebuild_id', sa.String(64), nullable=True))
    op.create_table('memory_flights',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('epoch', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('status', sa.String(20), nullable=False))
    op.create_index('ix_memory_flights_workspace_id', 'memory_flights', ['workspace_id'])
    connection = op.get_bind()
    legacy = bool(connection.execute(sa.text("SELECT count(*) FROM policies WHERE purpose IN ('extraction','embedding','rerank')")).scalar())
    epoch = connection.execute(sa.text("SELECT version FROM policies WHERE purpose='embedding'")).scalar() or 0
    table = sa.table('memory_configuration', sa.column('workspace_id'), sa.column('mode'), sa.column('version'),
        sa.column('epoch'), sa.column('embedding_fingerprint'), sa.column('phase'), sa.column('verification', sa.JSON()), sa.column('pending', sa.JSON()))
    for workspace in connection.execute(sa.text('SELECT DISTINCT workspace_id FROM users')).scalars():
        connection.execute(table.insert().values(workspace_id=workspace, mode='advanced' if legacy else 'simple',
            version=1, epoch=epoch, embedding_fingerprint='', phase='ready', verification={}, pending={}))
    from sqlalchemy.orm import Session
    from lingxi.memory_settings import MemoryConfiguration, selection
    with Session(bind=connection) as db:
        for row in db.scalars(sa.select(MemoryConfiguration)):
            row.embedding_fingerprint = selection(db, 'embedding', row.workspace_id)['fingerprint']
        db.flush()


def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM memory_configuration')).scalar():
        raise RuntimeError('请使用升级前备份与配套镜像整体回退，不能删除索引代际信息')
    op.drop_table('memory_flights')
    op.drop_table('memory_configuration')
