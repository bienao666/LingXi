"""Initial schema, deliberately frozen independently of future ORM changes."""
from alembic import op
import sqlalchemy as sa

revision = '0001'
down_revision = None


def upgrade():
    # The initial immutable SQLAlchemy metadata snapshot is stored as checked-in JSON.
    import json
    from pathlib import Path
    spec = json.loads((Path(__file__).parent / '0001_schema.json').read_text(encoding='utf-8'))
    types = {'String': sa.String, 'Text': sa.Text, 'Float': sa.Float, 'Integer': sa.Integer, 'Boolean': sa.Boolean, 'JSON': sa.JSON}
    for table in spec:
        columns = [sa.Column(c['name'], types[c['type']](c['length']) if c.get('length') else types[c['type']](),
                             primary_key=c['primary_key'], nullable=c['nullable']) for c in table['columns']]
        constraints = [sa.UniqueConstraint(*names) for names in table['unique']]
        op.create_table(table['name'], *columns, *constraints)
        for index in table['indexes']:
            op.create_index(index['name'], table['name'], index['columns'], unique=index['unique'])


def downgrade():
    import json
    from pathlib import Path
    spec = json.loads((Path(__file__).parent / '0001_schema.json').read_text(encoding='utf-8'))
    for table in reversed(spec):
        op.drop_table(table['name'])
