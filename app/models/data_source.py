import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.entity import IDENTIFIER_PATTERN
from app.models.mixins import (
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    parent_fk,
    tenant_unique,
)

if TYPE_CHECKING:
    from app.models.semantic_model_version import SemanticModelVersion
    from app.models.workspace import Workspace


class DataSourceDialect(enum.StrEnum):
    POSTGRESQL = "postgresql"


class DataSource(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """Connection details for a customer's warehouse — the data plane.

    This table holds credentials to databases we do not own, which makes it the
    highest-value row in the system. The password is stored only as ciphertext;
    `app.core.crypto` holds the key and `app.core.hostpolicy` decides whether a
    host may be reached at all.
    """

    __tablename__ = "data_sources"
    __table_args__ = (
        tenant_unique("data_sources"),
        parent_fk("data_sources", "workspace_id", "workspaces"),
        UniqueConstraint("workspace_id", "name", name="uq_data_sources_workspace_id_name"),
        CheckConstraint("port > 0 AND port <= 65535", name="port_range"),
        CheckConstraint("length(host) > 0", name="host_present"),
        # default_schema is interpolated into compiled SQL as an identifier;
        # database and username only ever reach a URL builder that escapes them.
        CheckConstraint(
            f"default_schema ~ '{IDENTIFIER_PATTERN}'", name="schema_ident"
        ),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), index=True)
    name: Mapped[str] = mapped_column(String(255))

    dialect: Mapped[DataSourceDialect] = mapped_column(
        SAEnum(
            DataSourceDialect,
            name="data_source_dialect",
            native_enum=False,
            create_constraint=True,
            length=32,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        default=DataSourceDialect.POSTGRESQL,
        server_default=DataSourceDialect.POSTGRESQL.value,
    )

    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer, default=5432)
    database: Mapped[str] = mapped_column(String(255))
    username: Mapped[str] = mapped_column(String(255))

    password_ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    key_version: Mapped[int] = mapped_column(SmallInteger, default=1)

    default_schema: Mapped[str] = mapped_column(String(255), default="public")
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    workspace: Mapped["Workspace"] = relationship(back_populates="data_sources")
    semantic_model_versions: Mapped[list["SemanticModelVersion"]] = relationship(
        back_populates="data_source", viewonly=True
    )

    def __repr__(self) -> str:
        # Never interpolate credentials, even into a repr.
        return f"<DataSource id={self.id} name={self.name!r} {self.host}:{self.port}>"
