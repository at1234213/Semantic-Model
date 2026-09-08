"""Step 13 routes, now behind admin authentication (Step 29)."""

import uuid

from fastapi.testclient import TestClient


def test_unauthenticated_requests_are_refused(client: TestClient) -> None:
    """The gap open since Step 14: this used to delete tenants for anyone."""
    assert client.get("/api/tenants").status_code == 401
    assert client.post("/api/tenants", json={"name": "acme"}).status_code == 401
    assert client.delete(f"/api/tenants/{uuid.uuid4()}").status_code == 401


def test_a_bad_key_is_refused(client: TestClient) -> None:
    response = client.get("/api/tenants", headers={"Authorization": "Bearer sk_nonsense"})
    assert response.status_code == 401


def test_a_non_admin_key_is_forbidden(client: TestClient, make_tenant) -> None:
    _, headers = make_tenant("acme")
    assert client.get("/api/tenants", headers=headers).status_code == 403


def test_create_tenant(client: TestClient, admin_headers) -> None:
    response = client.post("/api/tenants", json={"name": "acme"}, headers=admin_headers)
    assert response.status_code == 201

    body = response.json()
    assert body["name"] == "acme"
    uuid.UUID(body["id"])
    assert body["created_at"] and body["updated_at"]


def test_duplicate_tenant_name_is_rejected(client: TestClient, admin_headers) -> None:
    client.post("/api/tenants", json={"name": "acme"}, headers=admin_headers)
    duplicate = client.post("/api/tenants", json={"name": "acme"}, headers=admin_headers)
    assert duplicate.status_code == 409


def test_blank_name_is_rejected(client: TestClient, admin_headers) -> None:
    response = client.post("/api/tenants", json={"name": "   "}, headers=admin_headers)
    assert response.status_code == 422


def test_get_tenant(client: TestClient, admin_headers) -> None:
    created = client.post(
        "/api/tenants", json={"name": "acme"}, headers=admin_headers
    ).json()
    response = client.get(f"/api/tenants/{created['id']}", headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


def test_get_unknown_tenant_is_404(client: TestClient, admin_headers) -> None:
    response = client.get(f"/api/tenants/{uuid.uuid4()}", headers=admin_headers)
    assert response.status_code == 404


def test_list_tenants(client: TestClient, admin_headers) -> None:
    client.post("/api/tenants", json={"name": "acme"}, headers=admin_headers)
    client.post("/api/tenants", json={"name": "globex"}, headers=admin_headers)
    names = {t["name"] for t in client.get("/api/tenants", headers=admin_headers).json()}
    assert {"acme", "globex"} <= names


def test_delete_tenant(client: TestClient, admin_headers) -> None:
    created = client.post(
        "/api/tenants", json={"name": "acme"}, headers=admin_headers
    ).json()
    assert client.delete(
        f"/api/tenants/{created['id']}", headers=admin_headers
    ).status_code == 204
    assert client.get(
        f"/api/tenants/{created['id']}", headers=admin_headers
    ).status_code == 404
