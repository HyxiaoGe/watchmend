# tests/test_api.py
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from sentinel.api import register_routes
from sentinel.store import Store


class FakeFeishu:
    def __init__(self):
        self.sent: list[dict] = []
        self.fail = False

    async def send(self, card: dict) -> None:
        if self.fail:
            raise RuntimeError("boom")
        self.sent.append(card)


@pytest.fixture
def env(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    feishu = FakeFeishu()
    app = FastAPI()
    app.state.store = store
    app.state.patrol_feishu = feishu
    app.state.services = ["auth", "grafana"]
    app.state.settings = SimpleNamespace(sentinel_diag_token="", sentinel_heartbeat_utc_offset=8)
    register_routes(app)
    yield app, store, feishu
    store.close()


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def _insert(store, **kw):
    base = dict(
        ts=1000,
        rule="mem_pressure",
        subject="swap",
        severity="warning",
        status="open",
        detail="d",
        payload_json=json.dumps({"pct": 83.4}),
        diagnosis_status="pending",
        cooldown_until=0,
    )
    base.update(kw)
    return store.insert_event(**base)


async def test_pending_lists_only_pending(env):
    app, store, _ = env
    eid = _insert(store)
    _insert(store, rule="disk_usage", subject="/", diagnosis_status="skipped")
    async with _client(app) as c:
        r = await c.get("/events/pending")
    assert r.status_code == 200
    events = r.json()["events"]
    assert [e["id"] for e in events] == [eid]
    assert events[0]["payload_json"] == json.dumps({"pct": 83.4})


async def test_post_diagnosis_done_sends_card_and_updates(env):
    app, store, feishu = env
    eid = _insert(store)
    body = {"status": "done", "diagnosis": {"summary": "s", "root_cause": "r"}}
    async with _client(app) as c:
        r = await c.post(f"/events/{eid}/diagnosis", json=body)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "card_sent": True}
    assert store.get_event(eid).diagnosis_status == "done"
    assert len(feishu.sent) == 1
    assert feishu.sent[0]["card"]["header"]["template"] == "blue"


async def test_post_diagnosis_card_failure_still_persists(env):
    app, store, feishu = env
    feishu.fail = True
    eid = _insert(store)
    async with _client(app) as c:
        r = await c.post(
            f"/events/{eid}/diagnosis",
            json={"status": "done", "diagnosis": {"summary": "s"}},
        )
    assert r.json() == {"ok": True, "card_sent": False}
    assert store.get_event(eid).diagnosis_status == "done"


async def test_post_diagnosis_failed_no_card(env):
    app, store, feishu = env
    eid = _insert(store)
    async with _client(app) as c:
        r = await c.post(f"/events/{eid}/diagnosis", json={"status": "failed"})
    assert r.json() == {"ok": True, "card_sent": False}
    assert feishu.sent == []


async def test_post_diagnosis_404(env):
    app, _, _ = env
    async with _client(app) as c:
        r = await c.post("/events/999/diagnosis", json={"status": "done"})
    assert r.status_code == 404


async def test_token_required_when_configured(env):
    app, store, _ = env
    app.state.settings.sentinel_diag_token = "sek"
    eid = _insert(store)
    async with _client(app) as c:
        r1 = await c.post(f"/events/{eid}/diagnosis", json={"status": "failed"})
        r2 = await c.post(
            f"/events/{eid}/diagnosis",
            json={"status": "failed"},
            headers={"X-Sentinel-Token": "sek"},
        )
        r3 = await c.get("/events/pending")  # 读端点不鉴权
    assert r1.status_code == 401
    assert r2.status_code == 200
    assert r3.status_code == 200


async def test_daily_data_shape(env):
    app, store, _ = env
    async with _client(app) as c:
        r = await c.get("/report/daily-data")
    data = r.json()
    assert set(data) == {"date", "services", "open_events", "resolved_24h"}
    assert {s["service"] for s in data["services"]} == {"auth", "grafana"}
    for s in data["services"]:
        assert {"service", "total", "ok_count", "p50_ms", "p95_ms", "baseline_p95_ms"} <= set(s)


async def test_post_summary_sends_card(env):
    app, _, feishu = env
    async with _client(app) as c:
        r = await c.post("/report/summary", json={"text": "一切平稳"})
    assert r.status_code == 200
    assert "一切平稳" in str(feishu.sent[0])
