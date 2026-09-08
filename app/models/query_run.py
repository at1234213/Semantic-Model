import enum
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Enum as SAEnum
from sqlalchemy import Integer, Numeric, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.mixins import (
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    parent_fk,
    tenant_unique,
)

if TYPE_CHECKING:
    from app.models.workspace import Workspace


class RunStatus(enum.StrEnum):
    COMPILED = "compiled"      # SQL produced, not executed
    EXECUTED = "executed"
    FAILED = "failed"


class QueryRun(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """An audit record of one question asked of the system.

    Records the SQL that was generated but never the rows that came back:
    results are the customer's data, and an audit log is not the place for a
    second copy of it. Parameter *values* are omitted for the same reason — the
    count is enough to reconstruct the shape of a query.
    """

    __tablename__ = "query_runs"
    __table_args__ = (
        tenant_unique("query_runs"),
        parent_fk("query_runs", "workspace_id", "workspaces"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), index=True)
    semantic_model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), index=True, default=None
    )
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), index=True, default=None
    )

    question: Mapped[str] = mapped_column(Text)
    status: Mapped[RunStatus] = mapped_column(
        SAEnum(
            RunStatus,
            name="run_status",
            native_enum=False,
            create_constraint=True,
            length=32,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        )
    )

    intent: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), default=None)
    compiled_sql: Mapped[str | None] = mapped_column(Text, default=None)
    parameter_count: Mapped[int] = mapped_column(Integer, default=0)

    attempts: Mapped[int] = mapped_column(Integer, default=0)
    row_count: Mapped[int | None] = mapped_column(Integer, default=None)
    duration_ms: Mapped[float | None] = mapped_column(Numeric(12, 2), default=None)
    problems: Mapped[list | None] = mapped_column(JSONB(none_as_null=True), default=None)

    workspace: Mapped["Workspace"] = relationship(back_populates="query_runs")

    def __repr__(self) -> str:
        return f"<QueryRun {self.status} {self.question[:40]!r}>"
