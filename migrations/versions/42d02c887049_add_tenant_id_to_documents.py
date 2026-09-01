"""add tenant_id to documents

Revision ID: 42d02c887049
Revises: e6becd0d6b89
Create Date: 2026-09-01 16:45:54.735626

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '42d02c887049'
down_revision: str | None = 'e6becd0d6b89'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Hand-corrected: autogenerate emitted the composite foreign key BEFORE the
    # unique constraint it references, which Postgres rejects.
    op.add_column("documents", sa.Column("tenant_id", sa.Uuid(), nullable=False))
    op.create_index(op.f("ix_documents_tenant_id"), "documents", ["tenant_id"], unique=False)

    # Must exist before anything can reference (id, tenant_id).
    op.create_unique_constraint("uq_workspaces_id_tenant_id", "workspaces", ["id", "tenant_id"])

    # Replace the single-column FK with the composite one, which additionally
    # guarantees a document's tenant_id matches its workspace's.
    op.drop_constraint("fk_documents_workspace_id_workspaces", "documents", type_="foreignkey")
    op.create_foreign_key(
        "fk_documents_workspace_id_tenant_id_workspaces",
        "documents",
        "workspaces",
        ["workspace_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    # Exact reverse of upgrade(): the referencing FK goes before the unique
    # constraint it depends on, or Postgres refuses the drop.
    op.drop_constraint(
        "fk_documents_workspace_id_tenant_id_workspaces", "documents", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_documents_workspace_id_workspaces",
        "documents",
        "workspaces",
        ["workspace_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint("uq_workspaces_id_tenant_id", "workspaces", type_="unique")
    op.drop_index(op.f("ix_documents_tenant_id"), table_name="documents")
    op.drop_column("documents", "tenant_id")
