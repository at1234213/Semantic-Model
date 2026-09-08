"""Step 13 routes, now scoped by the authenticated tenant rather than a header."""

import uuid

from fastapi.testclient import TestClient


def test_unauthenticated_requests_are_refused(client: TestClient) -> None:
    assert client.get("/api/workspaces").status_code == 401


def test_a_malformed_authorization_header_is_refused(client: TestClient) -> None:
    for value in ["", "Bearer", "Basic abc", "Bearer ", "sk_looks_like_a_key"]:
        response = client.get("/api/workspaces", headers={"Authorization": value})
        assert response.status_code == 401, value


def test_a_revoked_key_stops_working(client: TestClient, db_session, make_tenant) -> None:
    from app.models import ApiKey
    from app.services import api_keys

    tenant, headers = make_tenant("acme")
    assert client.get("/api/workspaces", headers=headers).status_code == 200

    key = db_session.query(ApiKey).filter(ApiKey.tenant_id == tenant.id).one()
    api_keys.revoke(db_session, key)
    assert client.get("/api/workspaces", headers=headers).status_code == 401


def test_create_and_list_workspace(client: TestClient, make_tenant) -> None:
    tenant, headers = make_tenant("acme")

    created = client.post("/api/workspaces", json={"name": "sales"}, headers=headers)
    assert created.status_code == 201
    assert created.json()["tenant_id"] == str(tenant.id)

    listed = client.get("/api/workspaces", headers=headers).json()
    assert [w["name"] for w in listed] == ["sales"]


def test_the_tenant_comes_from_the_key_not_the_request(
    client: TestClient, make_tenant
) -> None:
    """A caller can no longer name the tenant it wants to act as."""
    tenant, headers = make_tenant("acme")
    other = uuid.uuid4()

    created = client.post(
        "/api/workspaces", json={"name": "sales"},
        headers={**headers, "X-Tenant-ID": str(other)},
    )
    assert created.status_code == 201
    assert created.json()["tenant_id"] == str(tenant.id)


def test_duplicate_name_within_tenant_is_rejected(client: TestClient, make_tenant) -> None:
    _, headers = make_tenant("acme")
    first = client.post("/api/workspaces", json={"name": "sales"}, headers=headers)
    assert first.status_code == 201
    duplicate = client.post("/api/workspaces", json={"name": "sales"}, headers=headers)
    assert duplicate.status_code == 409


def test_same_name_in_different_tenants_is_allowed(client: TestClient, make_tenant) -> None:
    _, a = make_tenant("acme")
    _, b = make_tenant("globex")
    assert client.post("/api/workspaces", json={"name": "sales"}, headers=a).status_code == 201
    assert client.post("/api/workspaces", json={"name": "sales"}, headers=b).status_code == 201


def test_a_tenant_cannot_list_another_tenants_workspace(
    client: TestClient, make_tenant
) -> None:
    _, a = make_tenant("acme")
    _, b = make_tenant("globex")
    client.post("/api/workspaces", json={"name": "sales"}, headers=a)

    assert client.get("/api/workspaces", headers=b).json() == []


def test_a_tenant_cannot_fetch_another_tenants_workspace(
    client: TestClient, make_tenant
) -> None:
    _, a = make_tenant("acme")
    _, b = make_tenant("globex")
    workspace_id = client.post(
        "/api/workspaces", json={"name": "sales"}, headers=a
    ).json()["id"]

    # 404, not 403: existence itself must not leak across tenants.
    assert client.get(f"/api/workspaces/{workspace_id}", headers=b).status_code == 404


def test_a_tenant_cannot_delete_another_tenants_workspace(
    client: TestClient, make_tenant
) -> None:
    _, a = make_tenant("acme")
    _, b = make_tenant("globex")
    workspace_id = client.post(
        "/api/workspaces", json={"name": "sales"}, headers=a
    ).json()["id"]

    assert client.delete(f"/api/workspaces/{workspace_id}", headers=b).status_code == 404
    assert client.get(f"/api/workspaces/{workspace_id}", headers=a).status_code == 200
