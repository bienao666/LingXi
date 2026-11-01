"""Guard attachment-aware delegation against downgrade to an executor that ignores references."""
import json
from alembic import op
import sqlalchemy as sa
revision = '0035'
down_revision = '0034'
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    for config in op.get_bind().execute(sa.text('SELECT config FROM agent_runs')).scalars():
        if isinstance(config, str):
            config = json.loads(config)
        if config.get('media_refs'):
            raise RuntimeError('已有协作附件来源记录，请恢复升级前备份及配套镜像，不能使用忽略来源的旧执行器')
