"""Daily spend metering + budget guards (pre-ship spec 2026-08-12)."""

from __future__ import annotations

import pytest

from dealbot import costs
from dealbot.costs import SpendMeter, estimate_call_cost, price_for_model


# ---- pricing ---------------------------------------------------------------


def test_prices_by_model_family():
    assert price_for_model("us.anthropic.claude-sonnet-4-5-20250929-v1:0") == (3.00, 15.00)
    assert price_for_model("us.anthropic.claude-haiku-4-5-20251001-v1:0") == (1.00, 5.00)
    assert price_for_model("amazon.titan-embed-image-v1") == (0.80, 0.0)


def test_unknown_model_overcounts_toward_safety():
    assert price_for_model("mystery-model") == (3.00, 15.00)


def test_estimate_call_cost_arithmetic():
    # 1M in + 1M out on sonnet = $18 exactly.
    assert estimate_call_cost("sonnet", 1_000_000, 1_000_000) == pytest.approx(18.0)
    assert estimate_call_cost("haiku", 2_000, 500) == pytest.approx(0.0045)


# ---- meter (fake redis) ----------------------------------------------------


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, float] = {}

    async def incrbyfloat(self, key, amount):
        self.store[key] = self.store.get(key, 0.0) + amount

    async def incr(self, key):
        self.store[key] = int(self.store.get(key, 0)) + 1

    async def expire(self, key, ttl):
        pass

    async def get(self, key):
        v = self.store.get(key)
        return None if v is None else str(v).encode()


class _BrokenRedis:
    def __getattr__(self, name):
        async def _boom(*a, **k):
            raise ConnectionError("redis down")

        return _boom


@pytest.mark.asyncio
async def test_meter_accumulates_and_gates():
    meter = SpendMeter(_FakeRedis())
    await meter.record_llm("sonnet", 1_000_000, 1_000_000)     # $18
    assert await meter.llm_spend_today() == pytest.approx(18.0)
    assert await meter.llm_budget_ok() is True                  # under $25

    await meter.record_llm("sonnet", 1_000_000, 0)              # +$3 → $21
    await meter.record_llm("sonnet", 2_000_000, 0)              # +$6 → $27
    assert await meter.llm_budget_ok() is False                 # over $25
    assert await meter.llm_budget_ok(factor=1.5) is True        # under $37.50


@pytest.mark.asyncio
async def test_session_cap(monkeypatch):
    monkeypatch.setattr(costs, "DAILY_BROWSER_SESSION_CAP", 2)
    meter = SpendMeter(_FakeRedis())
    assert await meter.session_cap_ok() is True
    await meter.record_session()
    await meter.record_session()
    assert await meter.session_cap_ok() is False


@pytest.mark.asyncio
async def test_bb_monthly_cap(monkeypatch):
    monkeypatch.setattr(costs, "BROWSERBASE_MONTHLY_SESSION_CAP", 2)
    meter = SpendMeter(_FakeRedis())
    assert await meter.bb_month_cap_ok() is True
    await meter.record_bb_session()
    await meter.record_bb_session()
    assert await meter.bb_month_cap_ok() is False
    assert await meter.bb_sessions_month() == 2


@pytest.mark.asyncio
async def test_bb_monthly_cap_fails_open():
    meter = SpendMeter(_BrokenRedis())
    assert await meter.bb_month_cap_ok() is True


@pytest.mark.asyncio
async def test_guards_fail_open_on_redis_errors():
    meter = SpendMeter(_BrokenRedis())
    assert await meter.llm_budget_ok() is True
    assert await meter.session_cap_ok() is True
    # Recording must swallow the failure too.
    await meter.record_llm("sonnet", 100, 100)
    await meter.record_session()


def test_fleet_paused_env(monkeypatch):
    monkeypatch.delenv("FLEET_PAUSED", raising=False)
    assert costs.fleet_paused() is False
    monkeypatch.setenv("FLEET_PAUSED", "1")
    assert costs.fleet_paused() is True


# ---- hunt gate -------------------------------------------------------------


@pytest.mark.asyncio
async def test_hunt_skips_when_budget_blown(monkeypatch):
    import dealbot.worker.tasks as tasks_mod

    class _BlownMeter:
        async def llm_budget_ok(self, factor=1.0):
            return False

    monkeypatch.setattr("dealbot.costs.build_meter", lambda: _BlownMeter())

    # Context load happens before the guard; stub the session to return a
    # minimal watchlist so the guard is what decides.
    from contextlib import asynccontextmanager

    class _WL:
        context = '{"product_query": "aeron"}'
        user_id = 1

    class _User:
        is_pro = True
        email = "a@t.com"

    class _Session:
        async def get(self, model, pk):
            from dealbot.db.models import User as UserModel

            return _User() if model is UserModel else _WL()

    @asynccontextmanager
    async def _session():
        yield _Session()

    monkeypatch.setattr(tasks_mod, "get_async_session", _session)
    result = await tasks_mod._run_hunt_and_persist(1)
    assert result == {"watchlist_id": 1, "skipped": "budget"}


@pytest.mark.asyncio
async def test_hunt_skips_when_paused(monkeypatch):
    import dealbot.worker.tasks as tasks_mod

    monkeypatch.setenv("FLEET_PAUSED", "1")

    from contextlib import asynccontextmanager

    class _WL:
        context = '{"product_query": "aeron"}'
        user_id = 1

    class _User:
        is_pro = True
        email = "a@t.com"

    class _Session:
        async def get(self, model, pk):
            from dealbot.db.models import User as UserModel

            return _User() if model is UserModel else _WL()

    @asynccontextmanager
    async def _session():
        yield _Session()

    monkeypatch.setattr(tasks_mod, "get_async_session", _session)
    result = await tasks_mod._run_hunt_and_persist(1)
    assert result == {"watchlist_id": 1, "skipped": "fleet_paused"}
