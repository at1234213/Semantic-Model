from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKeyConstraint, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.mixins import (
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    tenant_unique,
)

if TYPE_CHECKING:
    from app.models.tenant import Tenant

KEY_PREFIX = "sk_"
PREFIX_LENGTH = 8


class ApiKey(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """A credential that identifies the calling tenant.

    Only a SHA-256 digest is stored. The key itself is high-entropy random, so a
    plain digest is enough — a password KDF exists to slow down guessing at
    low-entropy secrets, and there is nothing here to guess.

    `prefix` is the first few characters of the key, kept in clear so a lookup
    can find the one candidate row before doing a constant-time comparison, and
    so a person can recognise their own key in a list.
    """

    __tablename__ = "api_keys"
    __table_args__ = (
        tenant_unique("api_keys"),
        # tenants is the root table and has no tenant_id of its own, so the
        # composite parent_fk helper does not apply here — a plain foreign key
        # on tenant_id is the whole relationship, as on workspaces.
        ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_api_keys_tenant_id",
            ondelete="CASCADE",
        ),
        CheckConstraint("length(key_hash) = 64", name="key_hash_is_sha256"),
        CheckConstraint("length(trim(name)) > 0", name="name_present"),
    )

    name: Mapped[str] = mapped_column(String(255))
    prefix: Mapped[str] = mapped_column(String(16), index=True)
    key_hash: Mapped[str] = mapped_column(String(64))

    # Admin keys reach the tenant-management routes; ordinary keys do not.
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="api_keys", viewonly=True)

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    def __repr__(self) -> str:
        return f"<ApiKey {self.name!r} {KEY_PREFIX}{self.prefix}… admin={self.is_admin}>"
