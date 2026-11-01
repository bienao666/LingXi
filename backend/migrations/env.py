from alembic import context
from lingxi.db import Base, engine
from lingxi import models, network, assistants, tasks, proactive, channels, delegation, media, voice, plugins, plugin_store, operations, auth_totp, backups, providers, chat_actions, branches, conversation_context
from lingxi import plugin_api, plugin_mcp, plugin_events, plugin_agents, plugin_oauth, channel_media
from lingxi import model_routing
from lingxi import emotions
from lingxi import channel_tasks, channel_artifacts
from lingxi import proactive_generation
from lingxi import usage_costs
from lingxi import model_budgets
from lingxi import media_policy, media_retention
from lingxi import system_settings, resources
from lingxi import workspace_migrations, voice_realtime, voice_native_output

def run_migrations():
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()

run_migrations()
