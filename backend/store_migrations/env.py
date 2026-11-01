from alembic import context
from lingxi_store.data import Base, engine
with engine().connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():context.run_migrations()
