"""The watchlist's intent embedding is the user's preference vector.

Two properties matter and neither held before Workstream C: it must be built
from the whole elicited context (not the bare product query), and it must be
rebuilt whenever that context is edited (a stale vector silently mis-retrieves).

These use `authed_client` rather than the real auth flow, so they run in the
default suite — tests/test_watchlists.py is integration-marked for Redis.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def no_celery_dispatch(monkeypatch):
    """create_watchlist fires research_for_agent.delay(); without a broker that
    retries against Redis for ~20s before the route's except swallows it."""
    from dealbot.worker import tasks

    monkeypatch.setattr(tasks.research_for_agent, "delay", lambda *a, **k: None)


def _create(client, **context_overrides) -> int:
    context = {
        "product_query": "used laptop",
        "condition": [],
        "brands": [],
        "keywords": [],
    }
    context.update(context_overrides)
    resp = client.post("/watchlists", json={"name": "Laptop", "context": context})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.fixture()
def captured_embeddings(monkeypatch):
    """Records every text handed to embed_text by the watchlists route."""
    captured: list[str] = []

    async def _capture(text: str) -> list[float]:
        captured.append(text)
        return [0.1] * 1536

    monkeypatch.setattr("dealbot.api.routes.watchlists.embed_text", _capture)
    return captured


def test_create_embeds_the_intent_document_not_the_bare_query(
    authed_client, captured_embeddings,
):
    _create(
        authed_client,
        max_budget=1200.0,
        buyer_profile="CS student who codes on the train; values battery life.",
    )
    assert captured_embeddings, "create must embed something"
    assert "CS student who codes on the train" in captured_embeddings[0], (
        "the profile must reach the embedding — embedding the bare query wastes it"
    )
    assert "used laptop" in captured_embeddings[0]


def test_patch_re_embeds_the_intent(authed_client, captured_embeddings):
    wl_id = _create(authed_client)
    captured_embeddings.clear()

    patched = authed_client.patch(
        f"/watchlists/{wl_id}",
        json={"buyer_profile": "Designer who needs colour accuracy above all."},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["context"]["buyer_profile"].startswith("Designer")
    assert captured_embeddings, (
        "editing context must re-embed — a stale vector describes the old intent"
    )
    assert "colour accuracy" in captured_embeddings[0]


def test_patch_keeps_the_old_vector_when_embedding_fails(authed_client, monkeypatch):
    """A transient embedding outage must not wipe a good vector. Keeping a
    slightly stale vector beats replacing it with nothing."""
    calls: list[str] = []

    async def _ok(text: str) -> list[float]:
        calls.append(text)
        return [0.5] * 1536

    monkeypatch.setattr("dealbot.api.routes.watchlists.embed_text", _ok)
    wl_id = _create(authed_client)

    async def _down(text: str) -> list[float]:
        calls.append(text)
        return []

    monkeypatch.setattr("dealbot.api.routes.watchlists.embed_text", _down)
    resp = authed_client.patch(f"/watchlists/{wl_id}", json={"max_budget": 900.0})

    assert resp.status_code == 200, resp.text
    assert resp.json()["context"]["max_budget"] == 900.0, "the edit still lands"
    assert len(calls) == 2, "it still attempts the re-embed"
