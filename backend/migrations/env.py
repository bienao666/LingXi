from alembic import context
from lingxi.db import Base, engine
from lingxi import models

def run_migrations():
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()

run_migrations()
