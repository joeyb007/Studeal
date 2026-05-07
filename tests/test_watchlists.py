from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def _register_and_login(client, email="joe@example.com", password="secret123") -> str:
    client.post("/auth/register", json={"email": email, "password": password})
    resp = client.post("/auth/token", data={"username": email, "password": password})
    return resp.json()["access_token"]


def test_create_watchlist(client):
    token = _register_and_login(client)
    resp = client.post(
        "/watchlists",
        json={"name": "Audio deals", "keywords": ["sony", "headphones"], "min_score": 60},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Audio deals"
    assert set(data["keywords"]) == {"sony", "headphones"}
    assert data["min_score"] == 60


def test_list_watchlists_empty(client):
    token = _register_and_login(client)
    resp = client.get("/watchlists", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_watchlists_returns_created(client):
    token = _register_and_login(client)
    client.post(
        "/watchlists",
        json={"name": "Gaming", "keywords": ["ps5", "xbox"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    resp = client.get("/watchlists", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["name"] == "Gaming"


def test_watchlists_require_auth(client):
    resp = client.get("/watchlists")
    assert resp.status_code == 401


def test_watchlists_isolated_per_user(client):
    token_a = _register_and_login(client, "a@example.com")
    token_b = _register_and_login(client, "b@example.com")
    client.post(
        "/watchlists",
        json={"name": "User A list", "keywords": ["deal"]},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    resp = client.get("/watchlists", headers={"Authorization": f"Bearer {token_b}"})
    assert resp.json() == []
