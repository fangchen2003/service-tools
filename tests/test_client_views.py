import asyncio
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx

from app import main
from app.client_views import subscription_payload
from app.config import Settings
from app.state import GateState


class DB:
    def __init__(self):
        self.used = {1: 20, 2: 80}
        self.v5 = {1: 2, 2: 9}
        self.global_v5 = 147
        self.key = dict(id=1, name="fixture", enabled=True, expires_at=0,
                        monthly_anlas=100, daily_v5=10, allow_anlas=True,
                        image_model_scope="all", is_admin=False, exclude_global_v5=False)

    async def month_anlas(self, key_id, _month):
        return self.used[key_id]

    async def get_counter(self, key_id, _day):
        return {"v5": self.v5[key_id]}

    async def get_setting(self, _name, default):
        return default

    async def day_v5_total(self, _day):
        return self.global_v5

    async def get_key_by_token(self, token):
        return self.key if token == "test-only" else None

    async def touch_key(self, _key_id):
        pass


def state():
    return SimpleNamespace(db=DB(), day=lambda *_: "2026-09-22",
                           month=lambda *_: "2026-09", tz=ZoneInfo("Asia/Shanghai"),
                           settings=SimpleNamespace(global_daily_v5=150))


def test_view_limits_by_current_key_and_shared_v5_without_upstream_account():
    async def run():
        st = state()
        first = await subscription_payload(st, st.db.key)
        second = await subscription_payload(st, st.db.key | {"id": 2})
        assert first["naiGate"]["anlasLeft"] == 80
        assert second["naiGate"]["anlasLeft"] == 20
        assert first["naiGate"]["v5LeftToday"] == 3
        assert second["naiGate"]["v5LeftToday"] == 1
        assert first["naiGate"]["account"] is None
        st.db.global_v5 = 150
        exhausted = await subscription_payload(st, st.db.key)
        exempt = await subscription_payload(st, st.db.key | {"exclude_global_v5": True})
        assert exhausted["naiGate"]["v5LeftToday"] == 0
        assert exempt["naiGate"]["v5LeftToday"] == 8
        # Official clients convert usage into weekly battery images/refill rates.
        # Local daily quotas must stay in naiGate instead of that optional field.
        assert all("usage" not in view for view in (first, second, exhausted, exempt))
    asyncio.run(run())


def test_admin_and_paid_disabled_views_preserve_author_permissions():
    async def run():
        st = state()
        disabled = await subscription_payload(st, st.db.key | {"allow_anlas": False})
        admin = await subscription_payload(st, st.db.key | {"is_admin": True, "allow_anlas": False})
        assert disabled["naiGate"]["anlasLeft"] == 0
        assert disabled["naiGate"]["anlasEnabled"] is False
        assert admin["naiGate"]["anlasEnabled"] is True
        assert admin["naiGate"]["anlasMonthlyLimit"] == 0
        assert admin["naiGate"]["v5Unlimited"] is True
    asyncio.run(run())


def test_compatibility_routes_require_key_and_keep_panel_schema(monkeypatch):
    async def run():
        st = state()
        monkeypatch.setattr(main, "STATE", st)
        paths = ["/user/subscription", "/user/data", "/user/information",
                 "/ai/user/subscription", "/ai/user/data", "/ai/user/information"]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://fixture") as client:
            for path in paths:
                assert (await client.get(path)).status_code == 401
                response = await client.get(path, headers={"Authorization": "Bearer test-only"})
                assert response.status_code == 200
                assert "test-only" not in response.text
                payload = response.json()
                assert "usage" not in payload.get("subscription", payload)
            sub = (await client.get(paths[0], headers={"Authorization": "Bearer test-only"})).json()
            gate = sub["naiGate"]
            assert isinstance(gate["anlasEnabled"], bool)
            for name in ("anlasLeft", "anlasMonthlyLimit", "v5LeftToday", "v5DailyLimit"):
                assert isinstance(gate[name], (int, float)) and gate[name] >= 0
    asyncio.run(run())


def test_queue_counts_include_pacing_wait_and_recover_on_cancel():
    async def run():
        st = GateState(Settings(nai_tokens=["fake-upstream"], key_image_min_interval=30))
        await st.wait_for_key_image_slot(1)
        waiting = asyncio.create_task(st.wait_for_key_image_slot(1))
        await asyncio.sleep(0)
        assert st.queue_snapshot()["global"]["waiting"] == 1
        waiting.cancel()
        try:
            await waiting
        except asyncio.CancelledError:
            pass
        assert st.queue_snapshot()["global"]["waiting"] == 0
        assert "fake-upstream" not in str(st.queue_snapshot())
    asyncio.run(run())
