"""Connections to the data plane — customer warehouses.

Nothing here touches the control-plane database. Every connection is opened
read-only and time-limited, enforced by the *customer's* Postgres rather than
by our compiler, so a bug in SQL generation still cannot write.
"""

from collections import OrderedDict

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL

from app.core.crypto import decrypt_secret
from app.core.hostpolicy import resolve_and_check
from app.models.data_source import DataSource

# -c options are applied by the server on connect. default_transaction_read_only
# makes every transaction reject writes regardless of the role's grants, and
# statement_timeout caps a runaway query at 30 seconds.
READ_ONLY_OPTIONS = "-c default_transaction_read_only=on -c statement_timeout=30000"

_MAX_CACHED_ENGINES = 32
_engines: OrderedDict[tuple, Engine] = OrderedDict()


def _cache_key(data_source: DataSource) -> tuple:
    """Any change to connection details produces a new engine."""
    return (
        data_source.id,
        data_source.host,
        data_source.port,
        data_source.database,
        data_source.username,
        data_source.key_version,
        data_source.updated_at,
    )


def build_url(data_source: DataSource) -> URL:
    """URL.create escapes each component, so passwords with @ or / are safe."""
    return URL.create(
        "postgresql+psycopg",
        username=data_source.username,
        password=decrypt_secret(data_source.password_ciphertext, data_source.key_version),
        host=data_source.host,
        port=data_source.port,
        database=data_source.database,
    )


def get_engine(data_source: DataSource) -> Engine:
    # Re-checked here as well as at write time: DNS can change in between, which
    # is exactly what a rebinding attack relies on.
    resolve_and_check(data_source.host, data_source.port)

    key = _cache_key(data_source)
    engine = _engines.pop(key, None)
    if engine is None:
        engine = create_engine(
            build_url(data_source),
            pool_pre_ping=True,
            pool_size=2,
            max_overflow=2,
            connect_args={"options": READ_ONLY_OPTIONS},
        )
    _engines[key] = engine

    while len(_engines) > _MAX_CACHED_ENGINES:
        _, evicted = _engines.popitem(last=False)
        evicted.dispose()
    return engine


def dispose_engines() -> None:
    while _engines:
        _, engine = _engines.popitem()
        engine.dispose()


def verify(data_source: DataSource) -> None:
    """Raises if the warehouse is unreachable or the credentials are wrong."""
    with get_engine(data_source).connect() as connection:
        connection.execute(text("SELECT 1"))


def list_tables(data_source: DataSource, schema: str | None = None) -> list[str]:
    with get_engine(data_source).connect() as connection:
        return list(
            connection.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = :schema ORDER BY table_name"
                ),
                {"schema": schema or data_source.default_schema},
            ).scalars()
        )


def list_columns(data_source: DataSource, table: str, schema: str | None = None) -> list[str]:
    with get_engine(data_source).connect() as connection:
        return list(
            connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = :schema AND table_name = :table "
                    "ORDER BY ordinal_position"
                ),
                {"schema": schema or data_source.default_schema, "table": table},
            ).scalars()
        )


def table_exists(data_source: DataSource, table: str, schema: str | None = None) -> bool:
    return bool(list_columns(data_source, table, schema))
