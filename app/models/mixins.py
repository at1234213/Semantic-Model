"""Shared declarative mixins.

Every table in the semantic layer wants the same primary key and audit
columns, so they live here rather than being repeated per model.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKeyConstraint, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column


class UUIDPrimaryKeyMixin:
    """UUID primary key, generated application-side."""

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    """created_at / updated_at maintained by the database clock."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class TenantScopedMixin:
    """Denormalised tenant_id, present on every table below `workspaces`.

    Every RLS policy compares this one indexed column. Kept honest by the
    composite foreign key each table declares to its parent — see `parent_fk`.
    """

    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), index=True)


def tenant_unique(table_name: str) -> UniqueConstraint:
    """UNIQUE (id, tenant_id) — the target this table's own children point at.

    Redundant with the primary key on its own, but Postgres requires a unique
    constraint on the exact column pair a composite foreign key references.
    """
    return UniqueConstraint("id", "tenant_id", name=_checked(f"uq_{table_name}_id_tenant_id"))


# Postgres truncates identifiers past this silently in some paths and errors in
# others. Fail loudly at import time instead of halfway through a migration.
MAX_IDENTIFIER_LENGTH = 63


def _checked(name: str) -> str:
    if len(name) > MAX_IDENTIFIER_LENGTH:
        raise ValueError(
            f"Constraint name {name!r} is {len(name)} characters, over Postgres's "
            f"{MAX_IDENTIFIER_LENGTH}-character limit. Shorten the table or column name."
        )
    return name


def parent_fk(table_name: str, column: str, parent_table: str) -> ForeignKeyConstraint:
    """Composite FK carrying tenant_id, so a row's tenant cannot disagree with its parent's.

    The name omits the tenant_id column deliberately: including it pushed
    `semantic_model_versions` past Postgres's identifier limit, and constraint
    names only need to be unique within their table.
    """
    return ForeignKeyConstraint(
        [column, "tenant_id"],
        [f"{parent_table}.id", f"{parent_table}.tenant_id"],
        name=_checked(f"fk_{table_name}_{column}_{parent_table}"),
        ondelete="CASCADE",
    )
