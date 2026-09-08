"""Mint the first admin API key.

Chicken-and-egg: every tenant route requires an admin key, and there is no way
to create the first one through the API it protects. This runs against the
database directly.

    docker compose exec api python -m scripts.bootstrap_admin_key "ops"

Run as a module, not a path: the script imports `app`, which is only on the
path when Python starts from /code.

The secret is printed once and never stored - only its SHA-256 digest is.
"""

import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import engine
from app.models.tenant import Tenant
from app.services import api_keys

BOOTSTRAP_TENANT = "__bootstrap__"


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "bootstrap-admin"

    with Session(engine) as session:
        tenant = session.scalars(
            select(Tenant).where(Tenant.name == BOOTSTRAP_TENANT)
        ).one_or_none()
        if tenant is None:
            tenant = Tenant(name=BOOTSTRAP_TENANT)
            session.add(tenant)
            session.flush()

        issued = api_keys.issue(session, tenant_id=tenant.id, name=name, is_admin=True)
        session.commit()

        print("Admin API key created. It is shown once and cannot be recovered.")
        print()
        print(f"  name   {issued.api_key.name}")
        print(f"  secret {issued.secret}")
        print()
        print("Send it as an Authorization: Bearer header.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
