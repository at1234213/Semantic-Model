import uuid

from fastapi.testclient import TestClient


def _make_tenant(client: TestClient, name: str) -> str:
    return client.post("/api/tenants", json={"name": name}).json()["id"]


def test_missing_tenant_header_is_400(client: TestClient) -> None:
    assert client.get("/api/workspaces").status_code == 400


def test_malformed_tenant_header_is_400(client: TestClient) -> None:
    response = client.get("/api/workspaces", headers={"X-Tenant-ID": "not-a-uuid"})
    assert response.status_code == 400


def test_unknown_tenant_is_404(client: TestClient) -> None:
    response = client.post(
        "/api/workspaces",
        json={"name": "sales"},
        headers={"X-Tenant-ID": str(uuid.uuid4())},
    )
    assert response.status_code == 404


def test_create_and_list_workspace(client: TestClient) -> None:
    tenant_id = _make_tenant(client, "acme")
    headers = {"X-Tenant-ID": tenant_id}

    created = client.post("/api/workspaces", json={"name": "sales"}, headers=headers)
    assert created.status_code == 201
    assert created.json()["tenant_id"] == tenant_id

    listed = client.get("/api/workspaces", headers=headers).json()
    assert [w["name"] for w in listed] == ["sales"]


def test_duplicate_name_within_tenant_is_rejected(client: TestClient) -> None:
    headers = {"X-Tenant-ID": _make_tenant(client, "acme")}
    first = client.post("/api/workspaces", json={"name": "sales"}, headers=headers)
    assert first.status_code == 201
    duplicate = client.post("/api/workspaces", json={"name": "sales"}, headers=headers)
    assert duplicate.status_code == 409


def test_same_name_in_different_tenants_is_allowed(client: TestClient) -> None:
    a = {"X-Tenant-ID": _make_tenant(client, "acme")}
    b = {"X-Tenant-ID": _make_tenant(client, "globex")}
    assert client.post("/api/workspaces", json={"name": "sales"}, headers=a).status_code == 201
    assert client.post("/api/workspaces", json={"name": "sales"}, headers=b).status_code == 201


def test_tenant_cannot_list_another_tenants_workspace(client: TestClient) -> None:
    a = {"X-Tenant-ID": _make_tenant(client, "acme")}
    b = {"X-Tenant-ID": _make_tenant(client, "globex")}
    client.post("/api/workspaces", json={"name": "sales"}, headers=a)

    assert client.get("/api/workspaces", headers=b).json() == []


def test_tenant_cannot_fetch_another_tenants_workspace(client: TestClient) -> None:
    a = {"X-Tenant-ID": _make_tenant(client, "acme")}
    b = {"X-Tenant-ID": _make_tenant(client, "globex")}
    workspace_id = client.post("/api/workspaces", json={"name": "sales"}, headers=a).json()["id"]

    # 404, not 403: existence itself must not leak across tenants.
    assert client.get(f"/api/workspaces/{workspace_id}", headers=b).status_code == 404


def test_tenant_cannot_delete_another_tenants_workspace(client: TestClient) -> None:
    a = {"X-Tenant-ID": _make_tenant(client, "acme")}
    b = {"X-Tenant-ID": _make_tenant(client, "globex")}
    workspace_id = client.post("/api/workspaces", json={"name": "sales"}, headers=a).json()["id"]

    assert client.delete(f"/api/workspaces/{workspace_id}", headers=b).status_code == 404
    assert client.get(f"/api/workspaces/{workspace_id}", headers=a).status_code == 200
