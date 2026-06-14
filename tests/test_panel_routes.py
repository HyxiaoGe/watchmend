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
    assert "证据台" in resp.text  # hdr.title
    assert "永不自动执行" in resp.text  # footer.readonly
    assert "组件健康" in resp.text  # sec.health
    assert "事件" in resp.text  # sec.events
    assert "Loki 巡检失败" in resp.text  # rule_label(scan_failed_loki, zh)
    store.close()


async def test_overview_health_bars_and_states(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from sentinel.models import ProbeSample

    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    tz = timezone(timedelta(hours=settings.sentinel_heartbeat_utc_offset))
    today = datetime.now(tz).date()
    d_ok = (today - timedelta(days=2)).isoformat()
    d_down = (today - timedelta(days=1)).isoformat()
    store.upsert_probe_daily("api", d_ok, total=100, ok_count=100, p50=10.0, p95=20.0)
    store.upsert_probe_daily("api", d_down, total=100, ok_count=10, p50=10.0, p95=20.0)  # →down
    store.add_probe_samples(
        [
            ProbeSample(
                ts=int(datetime.now(tz).timestamp()) - 30,
                service="api",
                ok=True,
                status_code=200,
                latency_ms=12.0,
            )
        ]
    )
    app = _build_app(store, settings)
    r = await _get(app, "/?win=30")
    assert r.status_code == 200
    assert "upbar" in r.text  # 柱条容器
    assert "down" in r.text  # 含 down 态格子(class 或图例)
    assert "api" in r.text  # 服务名出现
    store.close()


async def test_overview_en_light_smoke(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    app = _build_app(store, settings)
    r = await _get(app, "/?lang=en&theme=light")
    assert r.status_code == 200
    assert 'data-theme="light"' in r.text and '<html lang="en"' in r.text
    assert "Component Health" in r.text  # sec.health en
    assert "Events" in r.text  # sec.events en
    assert "证据台" not in r.text  # 外壳标题区已全英文
    store.close()


async def test_overview_event_ai_diagnosis_inline(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    settings = _settings(monkeypatch, LLM_BASE_URL="http://llm/v1", LLM_MODEL="m")
    store = Store(str(tmp_path / "s.db"))
    tz = timezone(timedelta(hours=settings.sentinel_heartbeat_utc_offset))
    recent = int(datetime.now(tz).timestamp()) - 3600  # 近 1 小时,落在事件流窗口内
    eid = store.insert_event(
        ts=recent,
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
        diagnosis_json=json.dumps({"summary": "OOM 根因摘要", "root_cause": "内存不足"}),
        tools_json=json.dumps([{"tool": "docker_logs", "args": {}, "output": "x", "ok": True}]),
    )
    store.resolve_event(eid, resolved_ts=recent + 600)  # 已恢复也必须可点
    app = _build_app(store, settings)
    r = await _get(app, "/")
    assert "OOM 根因摘要" in r.text  # AI summary 就地可见
    assert f"/event/{eid}" in r.text  # 已恢复事件有详情链接(可点)
    assert "AI 诊断" in r.text  # ev.ai chip
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


async def test_event_detail_i18n_and_theme(tmp_path, monkeypatch):
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
        diagnosis_status="skipped",
        cooldown_until=0,
    )
    app = _build_app(store, settings)
    r = await _get(app, f"/event/{eid}?lang=en&theme=light")
    assert r.status_code == 200
    assert 'data-theme="light"' in r.text and '<html lang="en"' in r.text
    assert "Container down" in r.text  # rule_label en
    assert "http-equiv" not in r.text  # 详情页不自动刷新
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


async def test_theme_and_lang_attrs_render(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    app = _build_app(store, settings)
    r = await _get(app, "/?theme=light&lang=en")
    assert 'data-theme="light"' in r.text
    assert '<html lang="en"' in r.text
    # 切换链接:三主题 + 双语 + 30/90 窗口(qurl 把 lang 放首位,故不带 ? 前缀断言)
    assert "theme=dark" in r.text and "theme=system" in r.text
    assert "lang=zh" in r.text and "lang=en" in r.text
    assert "win=30" in r.text
    store.close()


async def test_base_has_css_variables_and_light_palette(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    app = _build_app(store, settings)
    r = await _get(app, "/")
    assert "--st-ok" in r.text and "--bg" in r.text  # CSS 变量存在
    assert "data-theme=" in r.text
    assert "prefers-color-scheme" in r.text  # system 跟随 OS 的 media query
    store.close()


async def test_xss_service_and_detail_and_tooloutput_escaped(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from sentinel.models import ProbeSample

    settings = _settings(monkeypatch, LLM_BASE_URL="http://llm/v1", LLM_MODEL="m")
    store = Store(str(tmp_path / "s.db"))
    tz = timezone(timedelta(hours=settings.sentinel_heartbeat_utc_offset))
    recent = int(datetime.now(tz).timestamp()) - 30
    # 注入点:服务名(健康柱条)、事件 subject、event detail 文本、AI summary、tool output
    store.add_probe_samples(
        [
            ProbeSample(
                ts=recent,
                service="<script>svc</script>",
                ok=True,
                status_code=200,
                latency_ms=1.0,
            )
        ]
    )
    eid = store.insert_event(
        ts=recent,
        rule="container_down",
        subject="<script>sub</script>",
        severity="critical",
        status="open",
        detail="<img src=x onerror=alert(1)>",
        payload_json="{}",
        diagnosis_status="done",
        cooldown_until=0,
    )
    store.set_diagnosis(
        eid,
        status="done",
        diagnosis_json=json.dumps({"summary": "<b>x</b>"}),
        tools_json=json.dumps(
            [{"tool": "t", "args": {}, "output": "<script>out</script>", "ok": True}]
        ),
    )
    app = _build_app(store, settings)
    r_index = await _get(app, "/")
    r_detail = await _get(app, f"/event/{eid}")
    for body in (r_index.text, r_detail.text):
        assert "<script>svc</script>" not in body
        assert "<script>sub</script>" not in body
        assert "<script>out</script>" not in body
        assert "&lt;script&gt;" in body  # 实体化痕迹存在
    assert "onerror=alert(1)" not in r_index.text  # index 不渲染 detail 文本
    assert "&lt;img" in r_detail.text  # detail 文本被转义渲染
    store.close()


async def test_pagination_links_and_clamp(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    for i in range(20):  # > page_size(默认 8)→ 多页
        store.insert_event(
            ts=1700000000 + i,
            rule="service_down",
            subject="api",
            severity="critical",
            status="open",
            detail="d",
            payload_json="{}",
            diagnosis_status="skipped",
            cooldown_until=0,
        )
    app = _build_app(store, settings)
    r1 = await _get(app, "/?ev_page=1")
    assert "ev_page=2" in r1.text  # 有下一页链接
    r_over = await _get(app, "/?ev_page=999")
    assert r_over.status_code == 200  # 越界钳到末页,不崩
    store.close()


async def test_services_cap_expand_collapse(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from sentinel.models import ProbeSample

    settings = _settings(monkeypatch, SENTINEL_PANEL_SERVICES_CAP="2")
    store = Store(str(tmp_path / "s.db"))
    tz = timezone(timedelta(hours=settings.sentinel_heartbeat_utc_offset))
    recent = int(datetime.now(tz).timestamp()) - 30
    store.add_probe_samples(
        [
            ProbeSample(ts=recent, service=f"svc{i}", ok=True, status_code=200, latency_ms=1.0)
            for i in range(5)  # 5 > cap 2
        ]
    )
    app = _build_app(store, settings)
    r = await _get(app, "/")
    assert "svc_all=1" in r.text  # 折叠态有「展开剩余」链接
    r_all = await _get(app, "/?svc_all=1")
    assert "svc4" in r_all.text  # 展开后全列
    store.close()


async def test_overview_en_no_chinese_footer_leak(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    app = _build_app(store, settings)
    r = await _get(app, "/?lang=en")
    assert "(read-only)" in r.text  # footer.docker_readonly en
    assert "(只读)" not in r.text  # 不再泄漏中文
    store.close()


async def test_event_detail_404_per_lang(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    app = _build_app(store, settings)
    r_en = await _get(app, "/event/999999?lang=en")
    assert r_en.status_code == 404
    assert "Event not found" in r_en.text  # ev.notfound en
    assert "事件不存在" not in r_en.text  # 不再中英混排
    r_zh = await _get(app, "/event/999999?lang=zh")
    assert r_zh.status_code == 404
    assert "事件不存在" in r_zh.text  # zh 仍正常
    store.close()


async def test_default_lang_overrides_accept_language(tmp_path, monkeypatch):
    settings = _settings(monkeypatch, SENTINEL_PANEL_DEFAULT_LANG="en")
    store = Store(str(tmp_path / "s.db"))
    app = _build_app(store, settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.get("/", headers={"Accept-Language": "zh-CN,zh;q=0.9"})
    assert '<html lang="en"' in resp.text  # 配置默认 en 覆盖浏览器 zh(Codex 场景)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        resp2 = await c.get("/?lang=zh", headers={"Accept-Language": "en-US"})
    assert '<html lang="zh"' in resp2.text  # query 仍优先于配置默认
    store.close()


async def test_overview_today_nodata_tooltip(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    tz = timezone(timedelta(hours=settings.sentinel_heartbeat_utc_offset))
    yesterday = (datetime.now(tz).date() - timedelta(days=1)).isoformat()
    # 有历史日数据使服务出现,但今天无样本 → 今日格 nodata
    store.upsert_probe_daily("api", yesterday, total=10, ok_count=10, p50=1.0, p95=2.0)
    app = _build_app(store, settings)
    r = await _get(app, "/")
    assert "暂无样本" in r.text  # tip.today_nodata zh,今日无数据专属提示
    store.close()
