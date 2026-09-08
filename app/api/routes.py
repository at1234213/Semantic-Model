from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.db import get_db

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness: is the process up. Deliberately touches nothing else."""
    return {"status": "ok"}


@router.get("/ready")
def ready(db: Session = Depends(get_db)) -> dict[str, object]:
    """Readiness: can this instance actually serve a request.

    Checks the control-plane database and that the connection is the
    unprivileged role, because running as a superuser would silently disable
    every row-level security policy.
    """
    role, is_superuser = db.execute(
        text(
            "SELECT current_user, "
            "COALESCE((SELECT usesuper FROM pg_user WHERE usename = current_user), false)"
        )
    ).one()
    return {
        "status": "ready" if not is_superuser else "degraded",
        "database": "ok",
        "role": role,
        "row_level_security_effective": not is_superuser,
    }
