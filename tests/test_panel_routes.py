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


async def test_overview_llm_pill_from_config(tmp_path, monkeypatch):
    from sentinel.llm_config import LLMConfig

    path = tmp_path / "llm.yaml"
    path.write_text(
        "active: deepseek\nproviders:\n  deepseek:\n"
        "    base_url: https://api.deepseek.com/v1\n    model: deepseek-chat\n    api_key: ''\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SENTINEL_LLM_CONFIG_FILE", str(path))
    settings = _settings(monkeypatch)  # 无 LLM_BASE_URL/MODEL
    store = Store(str(tmp_path / "s.db"))
    app = _build_app(store, settings)
    app.state.llm_config = LLMConfig(settings)
    resp = await _get(app, "/")
    assert resp.status_code == 200
    assert "deepseek-chat" in resp.text  # 面板 LLM 标识反映 llm.yaml(env 留空也对)
    store.close()


async def test_overview_pending_restart_badge_and_open_lifecycle(tmp_path, monkeypatch):
    # 已配置(llm.yaml current 非空)但 diag job 启动时未注册 → 面板显示"诊断待重启",
    # 且 pending 事件标"open"(没人在查),不误标"调查中"。
    from sentinel.llm_config import LLMConfig

    path = tmp_path / "llm.yaml"
    path.write_text(
        "active: deepseek\nproviders:\n  deepseek:\n"
        "    base_url: https://api.deepseek.com/v1\n    model: deepseek-chat\n    api_key: ''\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SENTINEL_LLM_CONFIG_FILE", str(path))
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    store.insert_event(
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
    app = _build_app(store, settings)
    app.state.llm_config = LLMConfig(settings)
    app.state.diag_job_registered = False  # 启动后才配 → 未注册
    resp = await _get(app, "/")
    assert resp.status_code == 200
    assert "诊断待重启" in resp.text
    assert "investigating" not in resp.text  # pending 生命周期标 open,不是 investigating
    store.close()


async def test_pref_cookies_written_only_when_in_query(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    app = _build_app(store, settings)
    # 无偏好 querystring → 不写偏好 cookie
    r0 = await _get(app, "/")
    assert "wm_lang" not in r0.headers.get("set-cookie", "")
    # 带偏好 querystring → 写对应 cookie(SameSite=Lax)
    r1 = await _get(app, "/?lang=en&theme=light&win=30")
    sc = r1.headers.get("set-cookie", "")
    assert "wm_lang=en" in sc and "wm_theme=light" in sc and "wm_win=30" in sc
    assert "SameSite=Lax" in sc
    store.close()


async def test_pagination_and_window_params_accepted(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    app = _build_app(store, settings)
    # 越界页码 / 非法窗口 / svc_all 都不应 500(view 钳制 + prefs 回退)
    for path in ("/?ev_page=999", "/?ev_page=abc", "/?win=bogus", "/?svc_all=1"):
        resp = await _get(app, path)
        assert resp.status_code == 200
    store.close()


async def test_lang_cookie_survives_without_query(tmp_path, monkeypatch):
    # 仅 cookie 带 en(无 querystring)→ 不崩(en 文案断言留到模板重写后)
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    app = _build_app(store, settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://t", cookies={"wm_lang": "en"}
    ) as c:
        resp = await c.get("/")
    assert resp.status_code == 200
    store.close()
