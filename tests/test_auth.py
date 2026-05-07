from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def test_register_success(client):
    resp = client.post("/auth/register", json={"email": "joe@example.com", "password": "secret123"})
    assert resp.status_code == 201
    assert resp.json() == {"detail": "Account created"}


def test_register_duplicate_email(client):
    client.post("/auth/register", json={"email": "joe@example.com", "password": "secret123"})
    resp = client.post("/auth/register", json={"email": "joe@example.com", "password": "other"})
    assert resp.status_code == 409
    assert "already registered" in resp.json()["detail"]


def test_login_success(client):
    client.post("/auth/register", json={"email": "joe@example.com", "password": "secret123"})
    resp = client.post("/auth/token", data={"username": "joe@example.com", "password": "secret123"})
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"


def test_login_wrong_password(client):
    client.post("/auth/register", json={"email": "joe@example.com", "password": "secret123"})
    resp = client.post("/auth/token", data={"username": "joe@example.com", "password": "wrong"})
    assert resp.status_code == 401


def test_login_unknown_user(client):
    resp = client.post("/auth/token", data={"username": "nobody@example.com", "password": "x"})
    assert resp.status_code == 401
