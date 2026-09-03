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

    # "hash" needs no model download and is what the test suite uses;
    # "sentence_transformers" is the real encoder and needs the package.
    embedding_provider: str = "hash"
    # "scripted" needs no network or credentials and is what the tests use;
    # "claude" calls the Anthropic API and needs the package plus a key.
    intent_provider: str = "scripted"
    intent_model: str = "claude-opus-5"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_dim: int = 384

    secret_key: str = "change-me"

    # Encrypts warehouse credentials at rest. Deliberately separate from
    # secret_key: different purpose, different rotation cadence.
    credential_encryption_key: str = "change-me"

    # A tenant controls the host of a data source, which makes this an SSRF
    # primitive without a policy. None means "permissive in development only".
    data_source_allow_private_hosts: bool | None = None

    @property
    def allow_private_data_source_hosts(self) -> bool:
        if self.data_source_allow_private_hosts is not None:
            return self.data_source_allow_private_hosts
        return self.env == "development"


@lru_cache
def get_settings() -> Settings:
    return Settings()
