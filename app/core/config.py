from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "semantic-model"
    env: str = "development"
    debug: bool = True
    log_level: str = "info"

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/semantic_model"

    # Superuser connection. Used only by Alembic, which needs privileges the
    # runtime role must not have (CREATE ROLE, DDL, and Step 14's RLS policies).
    alembic_database_url: str = (
        "postgresql+psycopg://postgres:postgres@localhost:5432/semantic_model"
    )

    # The non-superuser role the API connects as. Postgres exempts superusers
    # from RLS, so the runtime role must be unprivileged for Step 14 to work.
    app_db_user: str = "app_user"
    app_db_password: str = "change-me"

    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_dim: int = 384

    secret_key: str = "change-me"


@lru_cache
def get_settings() -> Settings:
    return Settings()
