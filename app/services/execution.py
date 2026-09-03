"""Running a compiled query against a customer's warehouse.

Four independent things stop this writing anything:

  1. the compiler only ever assembles SELECT statements;
  2. `_assert_read_only` re-checks the text immediately before execution;
  3. the connection sets `default_transaction_read_only=on`, enforced by the
     customer's server rather than by us (Step 16b);
  4. the credentials should be a read-only role — the customer's choice, and
     the only one of the four we cannot verify.

Layers 1-3 hold even if the layer above fails. That is the point of having
more than one.
"""

import time
import uuid
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.data_source import DataSource
from app.models.semantic_model_version import SemanticModelVersion
from app.services.compiler import CompiledQuery
from app.services.warehouse import get_engine

# Fetched rows are held in memory, so this is a ceiling regardless of the
# LIMIT the compiler emitted.
MAX_FETCH_ROWS = 10_000


class ExecutionError(RuntimeError):
    """The query could not be run against the warehouse."""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple] = field(default_factory=list)
    truncated: bool = False
    duration_ms: float = 0.0

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def is_empty(self) -> bool:
        return not self.rows


def data_source_for(db: Session, version_id: uuid.UUID) -> DataSource:
    version = db.get(SemanticModelVersion, version_id)
    if version is None:
        raise ExecutionError("That semantic model version no longer exists")
    if version.data_source_id is None:
        raise ExecutionError(
            "This semantic model version is not connected to a data source, so "
            "there is nowhere to run the query."
        )
    source = db.get(DataSource, version.data_source_id)
    if source is None:
        raise ExecutionError("The configured data source no longer exists")
    return source


def execute(
    data_source: DataSource,
    compiled: CompiledQuery,
    *,
    max_rows: int = MAX_FETCH_ROWS,
) -> QueryResult:
    """Run a compiled query and return at most `max_rows` rows."""
    from app.services.compiler import _assert_read_only

    _assert_read_only(compiled.sql)

    engine = get_engine(data_source)
    started = time.perf_counter()
    try:
        with engine.connect() as connection:
            cursor = connection.execute(text(compiled.sql), compiled.parameters)
            rows = cursor.fetchmany(max_rows + 1)
            columns = list(cursor.keys())
    except DBAPIError as exc:
        original = getattr(exc, "orig", exc)
        raise ExecutionError(f"The warehouse rejected the query: {original}") from exc
    except SQLAlchemyError as exc:
        raise ExecutionError(f"Could not reach the warehouse: {exc}") from exc

    duration_ms = (time.perf_counter() - started) * 1000
    truncated = len(rows) > max_rows
    return QueryResult(
        columns=columns,
        rows=[tuple(row) for row in rows[:max_rows]],
        truncated=truncated,
        duration_ms=round(duration_ms, 2),
    )
