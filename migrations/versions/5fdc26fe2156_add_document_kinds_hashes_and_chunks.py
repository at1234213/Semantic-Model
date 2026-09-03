"""add document kinds hashes and chunks

Revision ID: 5fdc26fe2156
Revises: 9d1c87c2f6cc
Create Date: 2026-09-02 00:00:00.000000

Hand-ordered. Autogenerate produced three problems here:
  * it created uq_documents_id_tenant_id AFTER document_chunks, which
    references it, so the migration would have failed on first run;
  * it cannot see a constraint rename, so the old long foreign key name on
    documents would have survived;
  * it does not detect CHECK constraints added to an existing table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '5fdc26fe2156'
down_revision: str | None = '9d1c87c2f6cc'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RLS_TABLES = ("document_chunks",)
RLS_POLICY = "tenant_isolation"
RLS_PREDICATE = "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"

# documents was written before parent_fk() existed and still carries the old
# long naming scheme.
OLD_FK = "fk_documents_workspace_id_tenant_id_workspaces"
NEW_FK = "fk_documents_workspace_id"


def upgrade() -> None:
    op.execute(f"ALTER TABLE documents RENAME CONSTRAINT {OLD_FK} TO {NEW_FK}")

    op.add_column(
        "documents",
        sa.Column(
            "kind",
            sa.Enum(
                "note", "upload", "url",
                name="document_kind", native_enum=False,
                create_constraint=True, length=32,
            ),
            server_default="note",
            nullable=False,
        ),
    )
    op.add_column("documents", sa.Column("source_uri", sa.String(length=2048), nullable=True))
    op.add_column("documents", sa.Column("content_hash", sa.String(length=64), nullable=False))
    op.create_index(
        op.f("ix_documents_content_hash"), "documents", ["content_hash"], unique=False
    )

    op.create_check_constraint("title_present", "documents", "length(trim(title)) > 0")
    op.create_check_constraint(
        "content_hash_is_sha256", "documents", "length(content_hash) = 64"
    )

    # Must exist before document_chunks can reference (id, tenant_id).
    op.create_unique_constraint("uq_documents_id_tenant_id", "documents", ["id", "tenant_id"])

    op.create_table(
        "document_chunks",
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.CheckConstraint("char_count > 0", name=op.f("ck_document_chunks_char_count_positive")),
        sa.CheckConstraint(
            "length(source_hash) = 64", name=op.f("ck_document_chunks_source_hash_is_sha256")
        ),
        sa.CheckConstraint("ordinal >= 0", name=op.f("ck_document_chunks_ordinal_non_negative")),
        sa.ForeignKeyConstraint(
            ["document_id", "tenant_id"],
            ["documents.id", "documents.tenant_id"],
            name="fk_document_chunks_document_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_chunks")),
        sa.UniqueConstraint("document_id", "ordinal", name="uq_document_chunks_document_ordinal"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_document_chunks_id_tenant_id"),
    )
    op.create_index(
        op.f("ix_document_chunks_document_id"), "document_chunks", ["document_id"], unique=False
    )
    op.create_index(
        op.f("ix_document_chunks_tenant_id"), "document_chunks", ["tenant_id"], unique=False
    )

    for table in RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {RLS_POLICY} ON {table} "
            f"USING ({RLS_PREDICATE}) "
            f"WITH CHECK ({RLS_PREDICATE})"
        )


def downgrade() -> None:
    for table in reversed(RLS_TABLES):
        op.execute(f"DROP POLICY IF EXISTS {RLS_POLICY} ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.drop_index(op.f("ix_document_chunks_tenant_id"), table_name="document_chunks")
    op.drop_index(op.f("ix_document_chunks_document_id"), table_name="document_chunks")
    op.drop_table("document_chunks")

    op.drop_constraint("uq_documents_id_tenant_id", "documents", type_="unique")
    # Bare names: drop_constraint applies the ck_ convention.
    op.drop_constraint("content_hash_is_sha256", "documents", type_="check")
    op.drop_constraint("title_present", "documents", type_="check")
    op.drop_index(op.f("ix_documents_content_hash"), table_name="documents")
    op.drop_column("documents", "content_hash")
    op.drop_column("documents", "source_uri")
    op.drop_column("documents", "kind")

    op.execute(f"ALTER TABLE documents RENAME CONSTRAINT {NEW_FK} TO {OLD_FK}")
