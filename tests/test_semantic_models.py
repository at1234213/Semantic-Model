"""Step 15: semantic model identity, versions, and the invariants around them."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session

from app.models import SemanticModel, SemanticModelVersion, Tenant, VersionStatus, Workspace


def _scope(db: Session, tenant_id: uuid.UUID | str) -> None:
    db.execute(
        text("SELECT set_config('app.current_tenant_id', :t, true)"), {"t": str(tenant_id)}
    )


def _seed(db: Session, tenant_name: str) -> tuple[Tenant, Workspace, SemanticModel]:
    tenant = Tenant(name=tenant_name)
    db.add(tenant)
    db.flush()

    _scope(db, tenant.id)
    workspace = Workspace(tenant_id=tenant.id, name="analytics")
    db.add(workspace)
    db.flush()

    model = SemanticModel(
        tenant_id=tenant.id, workspace_id=workspace.id, name="Customer Analytics"
    )
    db.add(model)
    db.flush()
    return tenant, workspace, model


def _version(db: Session, model: SemanticModel, number: int, status: VersionStatus):
    version = SemanticModelVersion(
        tenant_id=model.tenant_id,
        semantic_model_id=model.id,
        version=number,
        status=status,
    )
    db.add(version)
    db.flush()
    return version


def test_version_defaults_to_draft(db_session: Session) -> None:
    _, _, model = _seed(db_session, "acme")
    version = SemanticModelVersion(
        tenant_id=model.tenant_id, semantic_model_id=model.id, version=1
    )
    db_session.add(version)
    db_session.flush()
    db_session.refresh(version)
    assert version.status == VersionStatus.DRAFT


def test_status_is_stored_as_its_value_not_its_name(db_session: Session) -> None:
    """SQLAlchemy would otherwise persist 'PUBLISHED' rather than 'published'."""
    _, _, model = _seed(db_session, "acme")
    _version(db_session, model, 1, VersionStatus.PUBLISHED)

    raw = db_session.execute(text("SELECT status FROM semantic_model_versions")).scalar_one()
    assert raw == "published"


def test_invalid_status_is_rejected_by_the_database(db_session: Session) -> None:
    _, _, model = _seed(db_session, "acme")
    with pytest.raises(IntegrityError):
        db_session.execute(
            text(
                "INSERT INTO semantic_model_versions "
                "(id, tenant_id, semantic_model_id, version, status, created_at, updated_at) "
                "VALUES (:i, :t, :m, 9, 'nonsense', now(), now())"
            ),
            {"i": str(uuid.uuid4()), "t": str(model.tenant_id), "m": str(model.id)},
        )


def test_only_one_published_version_per_model(db_session: Session) -> None:
    """The partial unique index replaces a current_version_id pointer."""
    _, _, model = _seed(db_session, "acme")
    _version(db_session, model, 1, VersionStatus.PUBLISHED)

    with pytest.raises(IntegrityError):
        _version(db_session, model, 2, VersionStatus.PUBLISHED)


def test_many_drafts_and_archives_are_allowed(db_session: Session) -> None:
    _, _, model = _seed(db_session, "acme")
    _version(db_session, model, 1, VersionStatus.ARCHIVED)
    _version(db_session, model, 2, VersionStatus.ARCHIVED)
    _version(db_session, model, 3, VersionStatus.DRAFT)
    _version(db_session, model, 4, VersionStatus.DRAFT)
    _version(db_session, model, 5, VersionStatus.PUBLISHED)

    count = db_session.execute(
        text("SELECT count(*) FROM semantic_model_versions")
    ).scalar_one()
    assert count == 5


def test_version_numbers_are_unique_per_model(db_session: Session) -> None:
    _, _, model = _seed(db_session, "acme")
    _version(db_session, model, 1, VersionStatus.DRAFT)
    with pytest.raises(IntegrityError):
        _version(db_session, model, 1, VersionStatus.ARCHIVED)


def test_version_cannot_claim_a_different_tenant(db_session: Session) -> None:
    """The composite FK makes tenant drift impossible, exactly as on documents."""
    _, _, model = _seed(db_session, "acme")
    other_tenant = Tenant(name="globex")
    db_session.add(other_tenant)
    db_session.flush()

    _scope(db_session, model.tenant_id)
    with pytest.raises((IntegrityError, ProgrammingError)):
        db_session.execute(
            text(
                "INSERT INTO semantic_model_versions "
                "(id, tenant_id, semantic_model_id, version, status, created_at, updated_at) "
                "VALUES (:i, :t, :m, 7, 'draft', now(), now())"
            ),
            {"i": str(uuid.uuid4()), "t": str(other_tenant.id), "m": str(model.id)},
        )


def test_rls_isolates_semantic_models(db_session: Session) -> None:
    acme, _, _ = _seed(db_session, "acme")
    _seed(db_session, "globex")

    _scope(db_session, acme.id)
    names = db_session.execute(text("SELECT name FROM semantic_models")).scalars().all()
    assert names == ["Customer Analytics"]

    _scope(db_session, "")
    assert db_session.execute(text("SELECT count(*) FROM semantic_models")).scalar_one() == 0


def test_rls_isolates_versions(db_session: Session) -> None:
    acme, _, acme_model = _seed(db_session, "acme")
    _, _, globex_model = _seed(db_session, "globex")
    _version(db_session, globex_model, 1, VersionStatus.PUBLISHED)

    _scope(db_session, acme.id)
    _version(db_session, acme_model, 1, VersionStatus.PUBLISHED)

    assert db_session.execute(
        text("SELECT count(*) FROM semantic_model_versions")
    ).scalar_one() == 1


def test_deleting_a_model_cascades_to_its_versions(db_session: Session) -> None:
    _, _, model = _seed(db_session, "acme")
    _version(db_session, model, 1, VersionStatus.DRAFT)
    _version(db_session, model, 2, VersionStatus.PUBLISHED)

    db_session.delete(model)
    db_session.flush()

    assert db_session.execute(
        text("SELECT count(*) FROM semantic_model_versions")
    ).scalar_one() == 0


def test_same_model_name_in_different_workspaces_is_allowed(db_session: Session) -> None:
    acme, _, _ = _seed(db_session, "acme")
    globex, _, _ = _seed(db_session, "globex")  # identical model name — must succeed

    for tenant in (acme, globex):
        _scope(db_session, tenant.id)
        assert db_session.execute(
            text("SELECT count(*) FROM semantic_models WHERE name = 'Customer Analytics'")
        ).scalar_one() == 1


def test_duplicate_model_name_in_one_workspace_is_rejected(db_session: Session) -> None:
    tenant, workspace, _ = _seed(db_session, "acme")

    duplicate = SemanticModel(
        tenant_id=tenant.id, workspace_id=workspace.id, name="Customer Analytics"
    )
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        db_session.flush()
