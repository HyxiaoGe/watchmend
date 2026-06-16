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
    assert "服务一览" in resp.text  # sec.services (renamed from sec.health in Task 7)
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


async def test_overview_health_row_uses_label_with_name_fallback(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from sentinel.models import ProbeSample

    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    tz = timezone(timedelta(hours=settings.sentinel_heartbeat_utc_offset))
    now = int(datetime.now(tz).timestamp())
    store.add_probe_samples(
        [
            ProbeSample(ts=now - 30, service="audio", ok=True, status_code=200, latency_ms=10.0),
            ProbeSample(ts=now - 30, service="auth", ok=True, status_code=200, latency_ms=10.0),
        ]
    )
    app = _build_app(store, settings)
    app.state.service_labels = {"audio": "Audio API"}  # 仅 audio 配显示名
    r = await _get(app, "/?win=30")
    assert r.status_code == 200
    assert "<b>Audio API</b>" in r.text  # 有 label → 健康行显示 label
    assert "<b>auth</b>" in r.text  # 无 label → 回退 name
    store.close()


async def test_overview_en_light_smoke(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    app = _build_app(store, settings)
    r = await _get(app, "/?lang=en&theme=light")
    assert r.status_code == 200
    assert 'data-theme="light"' in r.text and '<html lang="en"' in r.text
    assert "Services" in r.text  # sec.services en (renamed from sec.health in Task 7)
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


async def test_overview_links_carry_prefs(tmp_path, monkeypatch):
    # 事件详情链接与「最新」链接都携带 lang/theme/win，跳转不丢上下文（issue #11 claim 4）。
    from datetime import datetime, timedelta, timezone

    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    tz = timezone(timedelta(hours=settings.sentinel_heartbeat_utc_offset))
    recent = int(datetime.now(tz).timestamp()) - 3600
    eid = store.insert_event(
        ts=recent,
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
    # 用 win=90(非默认 30)：链接里出现 90 只能来自"读取了 query"，而非默认回退，
    # 否则该断言无法区分"honored query"与"fell back to default"。
    r = await _get(app, "/?lang=en&theme=light&win=90")
    # eurl 顺序 lang,theme,win；href 内 & 被 jinja 转义成 &amp;
    assert f"/event/{eid}?lang=en&amp;theme=light&amp;win=90" in r.text
    # 「最新」链接 qurl(ev_page=1) 沿用 lang/theme/win 再附 ev_page
    assert "?lang=en&amp;theme=light&amp;win=90&amp;ev_page=1" in r.text
    store.close()


async def test_event_detail_back_url_carries_prefs(tmp_path, monkeypatch):
    # 详情页「返回」链接携带 lang/theme/win，回到总览保持窗口/语言/主题（issue #11 claim 4）。
    settings = _settings(monkeypatch)
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
    r = await _get(app, f"/event/{eid}?lang=en&theme=light&win=30")
    assert 'href="/?lang=en&amp;theme=light&amp;win=30"' in r.text
    store.close()


async def test_services_cap_default_is_six(tmp_path, monkeypatch):
    # 不设 SENTINEL_PANEL_SERVICES_CAP 时默认仅展示 Top-6，第 7 个走「展开剩余」（issue #11）。
    from datetime import datetime, timedelta, timezone

    from sentinel.models import ProbeSample

    settings = _settings(monkeypatch)  # 用默认 cap，不覆盖
    store = Store(str(tmp_path / "s.db"))
    tz = timezone(timedelta(hours=settings.sentinel_heartbeat_utc_offset))
    recent = int(datetime.now(tz).timestamp()) - 30
    store.add_probe_samples(
        [
            ProbeSample(ts=recent, service=f"svc{i}", ok=True, status_code=200, latency_ms=1.0)
            for i in range(7)  # 7 > 默认 cap 6
        ]
    )
    app = _build_app(store, settings)
    r = await _get(app, "/")
    assert "svc_all=1" in r.text  # 折叠态有展开链接
    assert "展开剩余 1" in r.text  # n = 7 - 6，证明 cap 恰为 6
    assert "svc6" not in r.text  # 第 7 个(字母序末位)被折叠
    r_all = await _get(app, "/?svc_all=1")
    assert "svc6" in r_all.text  # 展开后全列
    store.close()


async def test_problem_service_survives_cap(tmp_path, monkeypatch):
    # D2 端到端守护：worst-first 排序 + cap 截断后,问题服务必须出现在首屏,
    # 而非被字母序挤出。防"先截断后排序/模板内排序"这类回归(issue #11 A6)。
    from datetime import datetime, timedelta, timezone

    from sentinel.models import ProbeSample

    settings = _settings(monkeypatch)  # 默认 cap=6
    store = Store(str(tmp_path / "s.db"))
    tz = timezone(timedelta(hours=settings.sentinel_heartbeat_utc_offset))
    recent = int(datetime.now(tz).timestamp()) - 30
    samples = [
        ProbeSample(ts=recent, service=f"ok{i}", ok=True, status_code=200, latency_ms=1.0)
        for i in range(7)  # 7 个全绿服务
    ]
    # 一个 down 服务,服务名字母序排在最后(zdown):若按字母序它会被 cap 挤出,
    # 但按现态最坏优先必须排到首位、留在首屏。
    samples += [
        ProbeSample(ts=recent, service="zdown", ok=True, status_code=200, latency_ms=1.0),
        ProbeSample(ts=recent - 1, service="zdown", ok=False, status_code=500, latency_ms=None),
        ProbeSample(ts=recent - 2, service="zdown", ok=False, status_code=500, latency_ms=None),
    ]  # 1 ok / 3 = 33% < 50% → down
    store.add_probe_samples(samples)
    app = _build_app(store, settings)
    r = await _get(app, "/")
    assert "zdown" in r.text  # 问题服务穿过 cap 留在首屏
    assert "ok6" not in r.text  # 字母序末位的健康服务被折叠掉
    r_all = await _get(app, "/?svc_all=1")
    assert "ok6" in r_all.text  # 展开后才出现
    store.close()


async def test_diag_lang_hint_shown_when_ui_lang_differs(tmp_path, monkeypatch):
    # UI 语言 ≠ 诊断生成语言(默认 zh) 时,总览内联与详情页都提示"原始诊断不回溯翻译";
    # 语言一致则不提示(issue #11 claim 5)。
    from datetime import datetime, timedelta, timezone

    settings = _settings(monkeypatch, LLM_BASE_URL="http://llm/v1", LLM_MODEL="m")
    store = Store(str(tmp_path / "s.db"))
    tz = timezone(timedelta(hours=settings.sentinel_heartbeat_utc_offset))
    recent = int(datetime.now(tz).timestamp()) - 3600
    eid = store.insert_event(
        ts=recent,
        rule="container_down",
        subject="postgres",
        severity="critical",
        status="open",
        detail="d",
        payload_json="{}",
        diagnosis_status="done",
        cooldown_until=0,
    )
    store.set_diagnosis(
        eid,
        status="done",
        diagnosis_json=json.dumps({"summary": "OOM 摘要", "root_cause": "内存不足"}),
        tools_json=None,
    )
    app = _build_app(store, settings)
    # 总览:en UI vs zh 诊断 → 显示英文提示;zh UI 一致 → 不显示
    r_en = await _get(app, "/?lang=en")
    assert "back-translated" in r_en.text
    r_zh = await _get(app, "/?lang=zh")
    assert "不回溯翻译" not in r_zh.text
    # 详情页同理
    d_en = await _get(app, f"/event/{eid}?lang=en")
    assert "back-translated" in d_en.text
    d_zh = await _get(app, f"/event/{eid}?lang=zh")
    assert "不回溯翻译" not in d_zh.text
    store.close()


async def test_default_window_is_30(tmp_path, monkeypatch):
    # 首屏无 query/cookie 时默认窗口降噪为 30d，而非历史上限 90d（issue #11 claim 1）。
    from datetime import datetime, timedelta, timezone

    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    tz = timezone(timedelta(hours=settings.sentinel_heartbeat_utc_offset))
    yest = (datetime.now(tz).date() - timedelta(days=1)).isoformat()
    # 出一个服务才会渲染逐日柱条的窗口轴
    store.upsert_probe_daily("api", yest, total=10, ok_count=10, p50=1.0, p95=2.0)
    app = _build_app(store, settings)
    r = await _get(app, "/")
    assert "30 天前" in r.text  # 逐日柱条窗口轴 axis.window(days=30)
    assert "90 天前" not in r.text  # 未回落到历史上限 90
    store.close()


async def test_hero_label_is_today_not_window(tmp_path, monkeypatch):
    # 回归 Codex P2:hero 大号与值同口径=今日(标签"今日可用率",不冒充"30 天可用率")。
    # 历史 29 天 100% + 今天单失败样本时,标签仍是今日;窗口历史由趋势线/逐日柱条承载。
    from datetime import datetime, timedelta, timezone

    from sentinel.models import ProbeSample

    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    tz = timezone(timedelta(hours=settings.sentinel_heartbeat_utc_offset))
    today = datetime.now(tz).date()
    for i in range(1, 30):
        store.upsert_probe_daily(
            "api",
            (today - timedelta(days=i)).isoformat(),
            total=100,
            ok_count=100,
            p50=10.0,
            p95=20.0,
        )
    now_ts = int(datetime.now(tz).timestamp())
    store.add_probe_samples(
        [ProbeSample(ts=now_ts - 60, service="api", ok=False, status_code=500, latency_ms=None)]
    )
    app = _build_app(store, settings)
    r = await _get(app, "/?win=30")
    assert r.status_code == 200
    assert "今日可用率" in r.text  # 标签=今日口径
    assert "30 天可用率" not in r.text  # 不再冒充窗口可用率
    assert "30 天前" in r.text  # 窗口故事仍在(逐日柱条窗口轴)
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


async def test_nav_tabs_render_with_overview_active(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    app = _build_app(store, settings)
    resp = await _get(app, "/")
    assert resp.status_code == 200
    assert 'class="tabs"' in resp.text  # 四标签导航壳已渲染
    assert "总览" in resp.text and "体检" in resp.text  # tab 文案
    # 四标签现均为真实跨页链接(Phase 2),不再有 tab-soon 占位
    assert "tab-soon" not in resp.text
    assert 'href="/services?' in resp.text  # 跨页链接
    assert 'href="/events?' in resp.text
    assert 'href="/hygiene?' in resp.text
    # 总览标签高亮
    assert "tab-on" in resp.text
    store.close()


async def test_badge_operational_when_no_open(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    app = _build_app(store, settings)
    resp = await _get(app, "/badge.svg")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/svg+xml")
    assert "operational" in resp.text
    assert "WatchMend" in resp.text
    store.close()


async def test_badge_incidents_when_open(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    store.insert_event(
        ts=1700000000,
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
    resp = await _get(app, "/badge.svg")
    assert resp.status_code == 200
    assert "1 incident" in resp.text
    store.close()


async def test_badge_not_registered_when_panel_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTINEL_PANEL_ENABLED", "false")
    settings = _settings(monkeypatch)  # 门控自读 env,不被 _settings 覆盖
    store = Store(str(tmp_path / "s.db"))
    app = _build_app(store, settings)  # register_panel_routes 自读 Settings() → 见 disabled
    resp = await _get(app, "/badge.svg")
    assert resp.status_code == 404
    store.close()


def test_badge_template_autoescapes_hostile_value():
    # 纵深防御:徽标 .svg 模板已纳入 autoescape——即便未来把不可信值(服务名/事件 subject)
    # 接进徽标,注入标签也会被转义而非破坏 SVG 结构。当前 handler 只喂服务端可控值,无活漏洞;
    # 本测试钉住 select_autoescape(["html","svg"]) 配置不被回退。
    from sentinel.panel.routes import _env

    out = _env.get_template("badge.svg").render(
        status_text='"/><script>alert(1)</script>',
        color="#3fb950",
        left_w=72,
        right_w=80,
        total_w=152,
        left_x=36.0,
        right_x=112.0,
    )
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


async def test_overview_renders_hero_and_service_sparkline(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from sentinel.models import ProbeSample

    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    tz = timezone(timedelta(hours=8))
    today = datetime.now(tz).date()
    now_ts = int(datetime.now(tz).timestamp())
    # 两天历史 + 今日样本,保证 health 非空、趋势线有 ≥2 个非空点
    store.upsert_probe_daily(
        "api", (today - timedelta(days=2)).isoformat(), total=100, ok_count=100, p50=10.0, p95=20.0
    )
    store.upsert_probe_daily(
        "api", (today - timedelta(days=1)).isoformat(), total=100, ok_count=98, p50=10.0, p95=22.0
    )
    store.add_probe_samples(
        [ProbeSample(ts=now_ts - 30, service="api", ok=True, status_code=200, latency_ms=11.0)]
    )

    app = _build_app(store, settings)
    resp = await _get(app, "/")
    assert resp.status_code == 200
    assert 'class="hero"' in resp.text  # 英雄区卡
    assert "ring-center" in resp.text  # 状态环中心数字
    assert "hero-trend" in resp.text  # 整体趋势线
    assert 'class="spark"' in resp.text  # 服务行迷你趋势线
    assert "服务一览" in resp.text  # sec.services
    # HERO 下方既有区块未丢(Phase 1 保留)
    assert "事件" in resp.text  # sec.events
    store.close()


async def test_services_list_renders(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from sentinel.models import ProbeSample

    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    tz = timezone(timedelta(hours=settings.sentinel_heartbeat_utc_offset))
    today = datetime.now(tz).date()
    now_ts = int(datetime.now(tz).timestamp())
    store.upsert_probe_daily(
        "api", (today - timedelta(days=1)).isoformat(), total=100, ok_count=100, p50=10.0, p95=22.0
    )
    store.add_probe_samples(
        [ProbeSample(ts=now_ts - 30, service="api", ok=True, status_code=200, latency_ms=12.0)]
    )
    app = _build_app(store, settings)
    app.state.service_labels = {"api": "API Gateway"}
    r = await _get(app, "/services?lang=zh")
    assert r.status_code == 200
    assert 'class="srow"' in r.text  # 服务行
    assert "API Gateway" in r.text  # 显示名 label
    assert "/service/api?" in r.text  # 整行点进服务详情(携带偏好)
    assert 'class="tab-on">服务</a>' in r.text  # 「服务」标签为当前页高亮
    store.close()


async def test_services_list_empty(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    app = _build_app(store, settings)
    r = await _get(app, "/services")
    assert r.status_code == 200
    assert "暂无服务数据" in r.text  # svc.empty
    store.close()


async def test_services_list_worst_first_and_xss(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from sentinel.models import ProbeSample

    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    tz = timezone(timedelta(hours=settings.sentinel_heartbeat_utc_offset))
    recent = int(datetime.now(tz).timestamp()) - 30
    store.add_probe_samples(
        [
            ProbeSample(ts=recent, service="ok-svc", ok=True, status_code=200, latency_ms=10.0),
            # down 服务(1 ok / 3 = 33% < 50%)
            ProbeSample(ts=recent, service="<x>svc", ok=True, status_code=200, latency_ms=10.0),
            ProbeSample(
                ts=recent - 1, service="<x>svc", ok=False, status_code=500, latency_ms=None
            ),
            ProbeSample(
                ts=recent - 2, service="<x>svc", ok=False, status_code=500, latency_ms=None
            ),
        ]
    )
    app = _build_app(store, settings)
    r = await _get(app, "/services")
    assert r.status_code == 200
    # 服务名转义(纵深防御:服务名进 sname 与 href)
    assert "<x>svc" not in r.text
    assert "&lt;x&gt;svc" in r.text
    # down 服务排到 ok 服务前(worst-first)
    assert r.text.index("&lt;x&gt;svc") < r.text.index("ok-svc")
    store.close()


async def test_service_detail_renders(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from sentinel.models import ProbeSample

    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    tz = timezone(timedelta(hours=settings.sentinel_heartbeat_utc_offset))
    today = datetime.now(tz).date()
    now_ts = int(datetime.now(tz).timestamp())
    for i in range(1, 8):
        store.upsert_probe_daily(
            "api",
            (today - timedelta(days=i)).isoformat(),
            total=10,
            ok_count=10,
            p50=10.0,
            p95=20.0,
        )
    store.add_probe_samples(
        [ProbeSample(ts=now_ts - 30, service="api", ok=True, status_code=200, latency_ms=18.0)]
    )
    app = _build_app(store, settings)
    app.state.service_labels = {"api": "API Gateway"}
    r = await _get(app, "/service/api?lang=zh")
    assert r.status_code == 200
    assert "API Gateway" in r.text  # 显示名 label
    assert 'class="crumb"' in r.text  # 面包屑回服务列表
    assert "/services?" in r.text
    assert 'class="lat-chart"' in r.text  # 延迟时序大图
    assert 'class="tab-on">服务</a>' in r.text  # 「服务」标签高亮
    assert '<meta http-equiv="refresh"' not in r.text  # 详情页不自动刷新
    store.close()


async def test_service_detail_404(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    app = _build_app(store, settings)
    r = await _get(app, "/service/ghost?lang=zh")
    assert r.status_code == 404
    assert "服务不存在" in r.text  # svc.notfound
    store.close()


async def test_service_detail_samples_toggle(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from sentinel.models import ProbeSample

    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    tz = timezone(timedelta(hours=settings.sentinel_heartbeat_utc_offset))
    now_ts = int(datetime.now(tz).timestamp())
    store.add_probe_samples(
        [
            ProbeSample(ts=now_ts - 60, service="api", ok=True, status_code=200, latency_ms=11.0),
            ProbeSample(ts=now_ts - 30, service="api", ok=True, status_code=200, latency_ms=13.0),
        ]
    )
    app = _build_app(store, settings)
    r = await _get(app, "/service/api?gran=samples")
    assert r.status_code == 200
    assert "gran=samples" in r.text  # 逐次/逐日切换链接保留
    store.close()


async def test_service_detail_name_escaped(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from sentinel.models import ProbeSample

    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    tz = timezone(timedelta(hours=settings.sentinel_heartbeat_utc_offset))
    now_ts = int(datetime.now(tz).timestamp())
    store.add_probe_samples(
        [ProbeSample(ts=now_ts - 30, service="<x>svc", ok=True, status_code=200, latency_ms=10.0)]
    )
    app = _build_app(store, settings)
    r = await _get(app, "/service/%3Cx%3Esvc")
    assert r.status_code == 200
    assert "<x>svc" not in r.text  # 服务名 h1 转义
    assert "&lt;x&gt;svc" in r.text
    store.close()


async def test_events_list_renders(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    tz = timezone(timedelta(hours=settings.sentinel_heartbeat_utc_offset))
    recent = int(datetime.now(tz).timestamp()) - 3600
    store.insert_event(
        ts=recent,
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
    app.state.service_labels = {"api": "API Gateway"}
    r = await _get(app, "/events?lang=zh")
    assert r.status_code == 200
    assert 'class="filters"' in r.text  # 筛选条
    assert "API Gateway" in r.text  # 服务显示名(卡片 + 筛选 chip)
    assert 'class="tab-on">事件</a>' in r.text  # 「事件」标签高亮
    store.close()


async def test_events_list_filter_by_severity(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    tz = timezone(timedelta(hours=settings.sentinel_heartbeat_utc_offset))
    recent = int(datetime.now(tz).timestamp()) - 3600
    store.insert_event(
        ts=recent,
        rule="service_down",
        subject="crit-svc",
        severity="critical",
        status="open",
        detail="d",
        payload_json="{}",
        diagnosis_status="skipped",
        cooldown_until=0,
    )
    store.insert_event(
        ts=recent - 1,
        rule="latency_degraded",
        subject="warn-svc",
        severity="warning",
        status="open",
        detail="d",
        payload_json="{}",
        diagnosis_status="skipped",
        cooldown_until=0,
    )
    app = _build_app(store, settings)
    r = await _get(app, "/events?severity=warning")
    assert r.status_code == 200
    # 服务筛选 chip 仍列出全部 subject(可切换),故按规则名判定卡片区:
    # 规则名只出现在事件卡,不出现在筛选 chip。warning 卡在,critical 卡被滤掉。
    assert "延迟退化" in r.text  # latency_degraded(warning)卡
    assert "服务异常" not in r.text  # service_down(critical)卡被滤掉
    store.close()


async def test_events_list_subject_escaped_and_filter_preserved(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    tz = timezone(timedelta(hours=settings.sentinel_heartbeat_utc_offset))
    recent = int(datetime.now(tz).timestamp()) - 3600
    store.insert_event(
        ts=recent,
        rule="service_down",
        subject="<x>svc",
        severity="critical",
        status="open",
        detail="d",
        payload_json="{}",
        diagnosis_status="skipped",
        cooldown_until=0,
    )
    app = _build_app(store, settings)
    r = await _get(app, "/events?severity=critical")
    assert r.status_code == 200
    assert "<x>svc" not in r.text  # 服务名转义(卡片 + 筛选 chip)
    assert "&lt;x&gt;svc" in r.text
    # 翻页/筛选链接保留 severity 选择(qurl 透传 transient)
    assert "severity=critical" in r.text
    store.close()
