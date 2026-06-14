# tests/test_panel_routes.py
import json

import httpx
from fastapi import FastAPI

from sentinel.config import Settings
from sentinel.panel.routes import register_panel_routes
from sentinel.store import Store


def _settings(monkeypatch, **env):
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)


def _build_app(store, settings, docker=None):
    app = FastAPI()
    app.state.store = store
    app.state.settings = settings
    app.state.docker = docker
    register_panel_routes(app)
    return app


async def _get(app, path):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.get(path)


async def test_overview_renders(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    store.insert_event(
        ts=1700000000,
        rule="scan_failed_loki",
        subject="log_scan",
        severity="warning",
        status="open",
        detail="d",
        payload_json="{}",
        diagnosis_status="skipped",
        cooldown_until=0,
    )
    app = _build_app(store, settings)
    resp = await _get(app, "/")
    assert resp.status_code == 200
    assert "证据台" in resp.text
    assert "永不自动执行" in resp.text
    assert "数据源故障" in resp.text  # scan_failed 标注
    store.close()


async def test_event_detail_shows_tool_chain(tmp_path, monkeypatch):
    settings = _settings(monkeypatch, LLM_BASE_URL="http://llm/v1", LLM_MODEL="m")
    store = Store(str(tmp_path / "s.db"))
    eid = store.insert_event(
        ts=1700000000,
        rule="container_down",
        subject="postgres",
        severity="critical",
        status="open",
        detail="d",
        payload_json="{}",
        diagnosis_status="pending",
        cooldown_until=0,
    )
    store.set_diagnosis(
        eid,
        status="done",
        diagnosis_json=json.dumps(
            {
                "summary": "OOM",
                "root_cause": "内存不足",
                "suggested_commands": ["docker restart postgres"],
                "confidence": "high",
            }
        ),
        tools_json=json.dumps(
            [
                {
                    "tool": "docker_logs",
                    "args": {"name": "postgres"},
                    "output": "FATAL: out of memory",
                    "ok": True,
                }
            ]
        ),
    )
    app = _build_app(store, settings)
    resp = await _get(app, f"/event/{eid}")
    assert resp.status_code == 200
    assert "docker_logs" in resp.text
    assert "FATAL: out of memory" in resp.text
    assert "<details>" in resp.text
    assert "docker restart postgres" in resp.text
    store.close()


async def test_event_detail_404(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    app = _build_app(store, settings)
    resp = await _get(app, "/event/999999")
    assert resp.status_code == 404
    assert "事件不存在" in resp.text
    store.close()


async def test_panel_disabled_does_not_register(tmp_path, monkeypatch):
    # register_panel_routes 内读 Settings() 看 flag(monkeypatch 优先于 .env)→ 关则不注册
    settings = _settings(monkeypatch, SENTINEL_PANEL_ENABLED="false")
    store = Store(str(tmp_path / "s.db"))
    app = _build_app(store, settings)
    resp = await _get(app, "/")
    assert resp.status_code == 404
    store.close()
