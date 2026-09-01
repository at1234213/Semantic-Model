import uuid

from fastapi.testclient import TestClient


def test_create_tenant(client: TestClient) -> None:
    response = client.post("/api/tenants", json={"name": "acme"})
    assert response.status_code == 201

    body = response.json()
    assert body["name"] == "acme"
    uuid.UUID(body["id"])  # raises if the server did not assign a real UUID
    assert body["created_at"] and body["updated_at"]


def test_duplicate_tenant_name_is_rejected(client: TestClient) -> None:
    assert client.post("/api/tenants", json={"name": "acme"}).status_code == 201
    duplicate = client.post("/api/tenants", json={"name": "acme"})
    assert duplicate.status_code == 409


def test_blank_name_is_rejected(client: TestClient) -> None:
    assert client.post("/api/tenants", json={"name": "   "}).status_code == 422


def test_get_tenant(client: TestClient) -> None:
    created = client.post("/api/tenants", json={"name": "acme"}).json()
    response = client.get(f"/api/tenants/{created['id']}")
    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


def test_get_unknown_tenant_is_404(client: TestClient) -> None:
    assert client.get(f"/api/tenants/{uuid.uuid4()}").status_code == 404


def test_list_tenants(client: TestClient) -> None:
    client.post("/api/tenants", json={"name": "acme"})
    client.post("/api/tenants", json={"name": "globex"})
    names = {t["name"] for t in client.get("/api/tenants").json()}
    assert {"acme", "globex"} <= names


def test_delete_tenant(client: TestClient) -> None:
    created = client.post("/api/tenants", json={"name": "acme"}).json()
    assert client.delete(f"/api/tenants/{created['id']}").status_code == 204
    assert client.get(f"/api/tenants/{created['id']}").status_code == 404
