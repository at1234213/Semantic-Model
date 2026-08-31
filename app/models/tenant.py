from typing import TYPE_CHECKING

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.workspace import Workspace


class Tenant(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Top-level isolation boundary. Every row in the system is reachable
    from exactly one tenant, which is what the Step 14 RLS policies key on.
    """

    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(String(255), unique=True)

    workspaces: Mapped[list["Workspace"]] = relationship(
        back_populates="tenant",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"<Tenant id={self.id} name={self.name!r}>"
