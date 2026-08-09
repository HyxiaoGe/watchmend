# tests/test_api.py
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from sentinel.api import register_routes
from sentinel.codex_hooks import CodexHookNotificationManager
from sentinel.notify.message import Kind
from sentinel.store import Store


class FakeBroadcaster:
    def __init__(self):
        self.sent: list = []
        self.fail = False

    async def send(self, n) -> int:
        self.sent.append(n)
        return 0 if self.fail else 1


@pytest.fixture
def env(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    bc = FakeBroadcaster()
    app = FastAPI()
    app.state.store = store
    app.state.patrol_broadcaster = bc
    app.state.services = ["auth", "grafana"]
    app.state.settings = SimpleNamespace(
        sentinel_diag_token="",
        sentinel_codex_ingest_token="",
        sentinel_codex_receipt_retention_days=30,
        sentinel_heartbeat_utc_offset=8,
    )
    app.state.codex_hook_manager = CodexHookNotificationManager(
        store=store,
        broadcaster=bc,
        grace_seconds=300,
        long_turn_seconds=180,
        utc_offset=8,
    )
    register_routes(app)
    yield app, store, bc
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


async def test_post_diagnosis_done_broadcasts_and_updates(env):
    app, store, bc = env
    eid = _insert(store)
    body = {"status": "done", "diagnosis": {"summary": "s", "root_cause": "r"}}
    async with _client(app) as c:
        r = await c.post(f"/events/{eid}/diagnosis", json=body)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "card_sent": True}
    assert store.get_event(eid).diagnosis_status == "done"
    assert len(bc.sent) == 1
    assert bc.sent[0].kind is Kind.DIAGNOSIS


async def test_post_diagnosis_broadcast_failure_still_persists(env):
    app, store, bc = env
    bc.fail = True
    eid = _insert(store)
    async with _client(app) as c:
        r = await c.post(
            f"/events/{eid}/diagnosis",
            json={"status": "done", "diagnosis": {"summary": "s"}},
        )
    assert r.json() == {"ok": True, "card_sent": False}  # 全渠道失败 → card_sent False
    assert store.get_event(eid).diagnosis_status == "done"  # 诊断仍落库


async def test_post_diagnosis_failed_no_card(env):
    app, store, bc = env
    eid = _insert(store)
    async with _client(app) as c:
        r = await c.post(f"/events/{eid}/diagnosis", json={"status": "failed"})
    assert r.json() == {"ok": True, "card_sent": False}
    assert bc.sent == []


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
        r3 = await c.get("/events/pending")
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


async def test_post_summary_broadcasts(env):
    app, _, bc = env
    async with _client(app) as c:
        r = await c.post("/report/summary", json={"text": "一切平稳"})
    assert r.status_code == 200
    assert bc.sent[0].kind is Kind.SUMMARY
    assert "一切平稳" in bc.sent[0].detail


def _codex_body(**kw):
    body = {
        "event_id": "thr_123:turn_456",
        "thread_id": "thr_123",
        "turn_id": "turn_456",
        "project": "watchmend",
        "cwd": "/workspace/watchmend",
        "task_summary": "接入 Codex 完成通知",
        "result_summary": "已完成本地修改并通过测试",
    }
    body.update(kw)
    return body


async def test_codex_notification_route_is_disabled_without_dedicated_token(env):
    app, _, bc = env
    async with _client(app) as c:
        r = await c.post("/notifications/codex", json=_codex_body())
    assert r.status_code == 404
    assert bc.sent == []


async def test_codex_notification_requires_dedicated_token(env):
    app, _, bc = env
    app.state.settings.sentinel_codex_ingest_token = "codex-secret"
    async with _client(app) as c:
        r = await c.post(
            "/notifications/codex",
            json=_codex_body(),
            headers={"X-WatchMend-Token": "wrong"},
        )
    assert r.status_code == 401
    assert bc.sent == []


async def test_codex_notification_rejects_mismatched_event_key(env):
    app, _, bc = env
    app.state.settings.sentinel_codex_ingest_token = "codex-secret"
    async with _client(app) as c:
        r = await c.post(
            "/notifications/codex",
            json=_codex_body(event_id="different"),
            headers={"X-WatchMend-Token": "codex-secret"},
        )
    assert r.status_code == 422
    assert bc.sent == []


async def test_codex_notification_broadcasts_as_non_alert_event(env):
    app, _, bc = env
    app.state.settings.sentinel_codex_ingest_token = "codex-secret"
    async with _client(app) as c:
        r = await c.post(
            "/notifications/codex",
            json=_codex_body(),
            headers={"X-WatchMend-Token": "codex-secret"},
        )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "delivered_count": 1, "duplicate": False}
    assert len(bc.sent) == 1
    assert bc.sent[0].kind is Kind.CODEX_TURN
    assert bc.sent[0].subject == "watchmend"


async def test_codex_notification_duplicate_is_not_broadcast_twice(env):
    app, _, bc = env
    app.state.settings.sentinel_codex_ingest_token = "codex-secret"
    headers = {"X-WatchMend-Token": "codex-secret"}
    async with _client(app) as c:
        first = await c.post("/notifications/codex", json=_codex_body(), headers=headers)
        second = await c.post("/notifications/codex", json=_codex_body(), headers=headers)
    assert first.status_code == 200
    assert second.json() == {"ok": True, "delivered_count": 1, "duplicate": True}
    assert len(bc.sent) == 1


async def test_codex_notification_all_channels_failed_is_retryable(env):
    app, _, bc = env
    app.state.settings.sentinel_codex_ingest_token = "codex-secret"
    bc.fail = True
    async with _client(app) as c:
        r = await c.post(
            "/notifications/codex",
            json=_codex_body(),
            headers={"X-WatchMend-Token": "codex-secret"},
        )
    assert r.status_code == 503
    assert r.json()["detail"] == "all notification channels failed"


async def test_codex_hook_endpoint_enqueues_without_immediate_broadcast(env):
    app, _, bc = env
    app.state.settings.sentinel_codex_ingest_token = "codex-secret"
    body = {
        "event_id": "session-1:turn-1:PermissionRequest:a",
        "event_name": "PermissionRequest",
        "session_id": "session-1",
        "turn_id": "turn-1",
        "project": "watchmend",
        "cwd": "/workspace/watchmend",
        "result_summary": "等待审批：部署到 dev",
        "tool_name": "Bash",
        "tool_fingerprint": "a" * 64,
    }
    async with _client(app) as c:
        r = await c.post(
            "/notifications/codex/hooks",
            json=body,
            headers={"X-WatchMend-Token": "codex-secret"},
        )
    assert r.status_code == 202
    assert r.json() == {"ok": True, "action": "queued", "category": "approval_required"}
    assert bc.sent == []
