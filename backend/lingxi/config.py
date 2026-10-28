from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_prefix='LINGXI_', extra='ignore')
    database_url: str = 'postgresql+psycopg://lingxi:lingxi@localhost:5432/lingxi'
    encryption_key: str = ''
    runtime_dir: Path = Path('runtime')
    cookie_secure: bool = True
    public_origin: str = 'https://localhost'
    hindsight_url: str = 'http://hindsight:8888'
    hindsight_api_key: str = ''
    memory_gateway_token: str = ''
    memory_embedding_dimensions: int = 1024
    allow_auto_memory: bool = True
    job_lease_seconds: int = 120
    provider_timeout: float = 45


@lru_cache
def settings() -> Settings:
    return Settings()
