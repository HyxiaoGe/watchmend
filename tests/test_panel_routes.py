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
    # 总览只保留概览级:待关注(当前未结告警)+ 汇总链接;明细去子页
    assert "待关注" in resp.text  # sec.attention
    assert "Loki 巡检失败" in resp.text  # 未结告警进待关注:rule_label(scan_failed_loki, zh)
    assert "log_scan" in resp.text  # 压缩行带服务展示名(无映射回退 subject),不留空
    assert "日报" in resp.text  # roll.report 汇总卡
    store.close()


async def test_services_down_from_daily_aggregate(tmp_path, monkeypatch):
    # 健康柱条/服务列表的 down 态可由历史 probe_daily 聚合派生(非仅实时样本)。明细在 /services。
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
    r = await _get(app, "/services?win=30")
    assert r.status_code == 200
    assert "down" in r.text  # 含 down 态(sdot class)
    assert "api" in r.text  # 服务名出现
    store.close()


async def test_services_row_uses_label_with_name_fallback(tmp_path, monkeypatch):
    # /services 每行展示 services.yaml 的 label;无 label 回退 name。
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
    r = await _get(app, "/services?win=30")
    assert r.status_code == 200
    assert ">Audio API</span>" in r.text  # 有 label → 服务行显示 label(sname)
    assert ">auth</span>" in r.text  # 无 label → 回退 name
    store.close()


async def test_overview_en_light_smoke(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    app = _build_app(store, settings)
    r = await _get(app, "/?lang=en&theme=light")
    assert r.status_code == 200
    assert 'data-theme="light"' in r.text and '<html lang="en"' in r.text
    assert "Services" in r.text  # tab.services en(导航壳标签)
    assert "Events" in r.text  # tab.events en(导航壳标签)
    assert "证据台" not in r.text  # 外壳标题区已全英文
    store.close()


async def test_events_ai_diagnosis_inline(tmp_path, monkeypatch):
    # 事件流就地展示 AI 诊断摘要,已恢复事件仍可点进详情。明细页是 /events。
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
    r = await _get(app, "/events")
    assert "OOM 根因摘要" in r.text  # AI summary 就地可见
    assert f"/event/{eid}" in r.text  # 已恢复事件有详情链接(可点)
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
    assert "deepseek-chat" not in resp.text  # 模型名/provider 不再暴露在面板
    assert "LLM:" not in resp.text  # 旧前缀去除
    assert "AI 诊断 ✓" in resp.text  # 改中性指示器(llm.yaml active → 诊断在跑)
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
    assert "AI 诊断" in resp.text and "待重启" in resp.text  # 中性指示 + 待重启态
    assert "deepseek-chat" not in resp.text  # 仍不泄露模型名
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
    r_services = await _get(app, "/services")  # 服务名注入点
    r_detail = await _get(app, f"/event/{eid}")  # subject/detail/summary/tool output 注入点
    assert "<script>svc</script>" not in r_services.text
    assert "&lt;script&gt;" in r_services.text  # 服务名实体化
    for body in (r_detail.text,):
        assert "<script>sub</script>" not in body
        assert "<script>out</script>" not in body
        assert "&lt;script&gt;" in body  # 实体化痕迹存在
    assert "&lt;img" in r_detail.text  # detail 文本被转义渲染
    store.close()


async def test_events_pagination_links_and_clamp(tmp_path, monkeypatch):
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
    r1 = await _get(app, "/events?ev_page=1")
    assert "ev_page=2" in r1.text  # 有下一页链接
    r_over = await _get(app, "/events?ev_page=999")
    assert r_over.status_code == 200  # 越界钳到末页,不崩
    store.close()


async def test_hygiene_en_no_chinese_docker_leak(tmp_path, monkeypatch):
    # docker 只读标记随姿态移到 /hygiene;en 下不泄漏中文。
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    app = _build_app(store, settings)
    r = await _get(app, "/hygiene?lang=en")
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
    # 待关注行 → 事件详情 eurl 顺序 lang,theme,win；href 内 & 被 jinja 转义成 &amp;
    assert f"/event/{eid}?lang=en&amp;theme=light&amp;win=90" in r.text
    # 汇总卡的子页链接 tab_url 也沿用 lang/theme/win
    assert "/services?lang=en&amp;theme=light&amp;win=90" in r.text
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


async def test_diag_lang_hint_shown_when_ui_lang_differs(tmp_path, monkeypatch):
    # UI 语言 ≠ 诊断生成语言(默认 zh) 时,事件流与详情页都提示"原始诊断不回溯翻译";
    # 语言一致则不提示(issue #11 claim 5)。诊断内联已移到 /events。
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
    # 事件流:en UI vs zh 诊断 → 显示英文提示;zh UI 一致 → 不显示
    r_en = await _get(app, "/events?lang=en")
    assert "back-translated" in r_en.text
    r_zh = await _get(app, "/events?lang=zh")
    assert "不回溯翻译" not in r_zh.text
    # 详情页同理
    d_en = await _get(app, f"/event/{eid}?lang=en")
    assert "back-translated" in d_en.text
    d_zh = await _get(app, f"/event/{eid}?lang=zh")
    assert "不回溯翻译" not in d_zh.text
    store.close()


async def test_default_window_is_30(tmp_path, monkeypatch):
    # 无 query/cookie 时默认窗口降噪为 30d，而非历史上限 90d（issue #11 claim 1）。
    # 窗口数现由 /services 的趋势列标题 svc.col_trend(days=) 承载。
    from datetime import datetime, timedelta, timezone

    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    tz = timezone(timedelta(hours=settings.sentinel_heartbeat_utc_offset))
    yest = (datetime.now(tz).date() - timedelta(days=1)).isoformat()
    store.upsert_probe_daily("api", yest, total=10, ok_count=10, p50=1.0, p95=2.0)
    app = _build_app(store, settings)
    r = await _get(app, "/services")
    assert "30 天 p95 趋势" in r.text  # 默认窗口 = 30
    assert "90 天 p95 趋势" not in r.text  # 未回落到历史上限 90
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
    assert "今日可用率" in r.text  # hero 标签=今日口径
    assert "30 天可用率" not in r.text  # 不再冒充窗口可用率
    store.close()


async def test_services_today_nodata_state(tmp_path, monkeypatch):
    # 有历史日数据使服务出现,但今天无样本 → /services 现态 nodata、今日可用率显示「—」。
    from datetime import datetime, timedelta, timezone

    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    tz = timezone(timedelta(hours=settings.sentinel_heartbeat_utc_offset))
    yesterday = (datetime.now(tz).date() - timedelta(days=1)).isoformat()
    store.upsert_probe_daily("api", yesterday, total=10, ok_count=10, p50=1.0, p95=2.0)
    app = _build_app(store, settings)
    r = await _get(app, "/services")
    assert "api" in r.text  # 服务仍出现
    assert "无数据" in r.text  # st.nodata:今日态 nodata(sdot title)
    assert "—" in r.text  # 今日可用率无值显示破折号
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
    assert 'class="kpi"' in resp.text  # SLO 看板环旁 KPI 事实(取代旧整体趋势线)
    # 每服务迷你趋势线现由 /services 行承载
    r_svc = await _get(app, "/services")
    assert 'class="sspark"' in r_svc.text  # 服务行迷你趋势线
    store.close()


async def test_overview_slo_board_shape(tmp_path, monkeypatch):
    # SLO 看板:环旁 KPI 事实(N/M ok + 7/30/90d 均值 + 开放 + 净流 + MTTR)、
    # 最差优先服务表(今日/7天/30天 + 整行点进 /service)、体检按类型 + 日报新鲜度;
    # 去重(无旧 .hstat 左侧重复大号)、弃用 days_clean(无「连续」文案)。
    from datetime import datetime, timedelta, timezone

    from sentinel.models import ProbeSample

    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    tz = timezone(timedelta(hours=settings.sentinel_heartbeat_utc_offset))
    today = datetime.now(tz).date()
    now_ts = int(datetime.now(tz).timestamp())
    # 一个服务,9 天历史 100%(让 7d/30d 窗口均值有值),今日有样本
    for i in range(1, 10):
        store.upsert_probe_daily(
            "api",
            (today - timedelta(days=i)).isoformat(),
            total=100,
            ok_count=100,
            p50=10.0,
            p95=20.0,
        )
    store.add_probe_samples(
        [ProbeSample(ts=now_ts - 30, service="api", ok=True, status_code=200, latency_ms=11.0)]
    )
    # 开放 hygiene 事件(证书)→ 进汇总体检计数,不进待关注
    store.insert_event(
        ts=now_ts - 3600,
        rule="cert_expiry",
        subject="a.com",
        severity="warning",
        status="open",
        detail="d",
        payload_json="{}",
        diagnosis_status="skipped",
        cooldown_until=0,
    )
    # 已恢复真实事件(MTTR 有值:10 分钟)
    eid = store.insert_event(
        ts=now_ts - 7200,
        rule="service_down",
        subject="db",
        severity="critical",
        status="resolved",
        detail="d",
        payload_json="{}",
        diagnosis_status="skipped",
        cooldown_until=0,
    )
    store.resolve_event(eid, resolved_ts=now_ts - 7200 + 600)

    app = _build_app(store, settings)
    r = await _get(app, "/?win=30")
    assert r.status_code == 200
    # HERO 去重 + days_clean 弃用
    assert 'class="hstat"' not in r.text  # 旧左侧重复大号容器已删
    assert "连续" not in r.text  # 不再有 days_clean「连续 N 天」文案
    # 环旁 KPI 事实
    assert 'class="kpi"' in r.text
    assert "7 天均值" in r.text and "30 天均值" in r.text  # ov.win_mean
    assert "MTTR" in r.text  # ov.mttr 标签
    assert "开放" in r.text  # ov.open
    # 服务表:表头 + 服务行整行点进详情
    assert "服务可用率" in r.text  # roster.title
    assert "7天" in r.text and "30天" in r.text  # roster.col_7d/30d
    assert "/service/api?" in r.text  # 整行点进服务详情(携带偏好)
    # 汇总行:体检按类型 + 日报
    assert "证书" in r.text  # ov.hyg_cert(cert_expiry 计入)
    assert "日报" in r.text  # roll.report
    store.close()


async def test_overview_ring_threshold_color_class(tmp_path, monkeypatch):
    # 今日可用率 50%（< partial 99.5）→ 环中心阈值色 class = bad（红）。
    from datetime import datetime, timedelta, timezone

    from sentinel.models import ProbeSample

    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    tz = timezone(timedelta(hours=settings.sentinel_heartbeat_utc_offset))
    now_ts = int(datetime.now(tz).timestamp())
    store.add_probe_samples(
        [
            ProbeSample(ts=now_ts - 10, service="api", ok=True, status_code=200, latency_ms=10.0),
            ProbeSample(ts=now_ts - 11, service="api", ok=False, status_code=500, latency_ms=None),
        ]
    )
    app = _build_app(store, settings)
    r = await _get(app, "/")
    assert r.status_code == 200
    assert 'class="ring-center bad"' in r.text  # 阈值色按 uptime_grade,非硬编码
    store.close()


async def test_overview_roster_state_tint_and_nodata_never_green(tmp_path, monkeypatch):
    # 服务表整行底色按现态着色;无数据窗口渲染灰 "–"，整行不染绿。
    from datetime import datetime, timedelta, timezone

    from sentinel.models import ProbeSample

    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    tz = timezone(timedelta(hours=settings.sentinel_heartbeat_utc_offset))
    today = datetime.now(tz).date()
    now_ts = int(datetime.now(tz).timestamp())
    # down 服务：今日 1 ok / 4 → 25% < 50% → down
    store.add_probe_samples(
        [
            ProbeSample(
                ts=now_ts - 10, service="downsvc", ok=True, status_code=200, latency_ms=9.0
            ),
            ProbeSample(
                ts=now_ts - 11, service="downsvc", ok=False, status_code=500, latency_ms=None
            ),
            ProbeSample(
                ts=now_ts - 12, service="downsvc", ok=False, status_code=500, latency_ms=None
            ),
            ProbeSample(
                ts=now_ts - 13, service="downsvc", ok=False, status_code=500, latency_ms=None
            ),
        ]
    )
    # nodata 服务：仅历史日数据，今日无样本 → 今日态 nodata、今日窗口无值
    store.upsert_probe_daily(
        "ndsvc", (today - timedelta(days=1)).isoformat(), total=10, ok_count=10, p50=1.0, p95=2.0
    )
    app = _build_app(store, settings)
    r = await _get(app, "/")
    assert r.status_code == 200
    assert 'class="rrow down"' in r.text  # 整行 down 态着色 class
    assert 'class="rrow nodata"' in r.text  # 无数据行（永不染绿）
    assert "rw nodata" in r.text  # 无数据窗口单元格灰 "–"，不显示为绿/100%
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
    assert 'class="tab-on" title="服务"' in r.text  # 「服务」标签为当前页高亮
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
    assert 'class="tab-on" title="服务"' in r.text  # 「服务」标签高亮
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
    assert 'class="tab-on" title="事件"' in r.text  # 「事件」标签高亮
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


async def test_event_detail_timeline_and_confidence_ring(tmp_path, monkeypatch):
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
                "summary": "进程被 OOM killer 终止",
                "root_cause": "内存超限",
                "confidence": "high",
                "suggested_commands": ["docker restart postgres"],
            }
        ),
        tools_json=json.dumps(
            [
                {"tool": "docker_logs", "args": {"name": "postgres"}, "output": "OOM", "ok": True},
                {"tool": "docker_inspect", "args": {}, "output": "{}", "ok": False},
            ]
        ),
    )
    app = _build_app(store, settings)
    r = await _get(app, f"/event/{eid}?lang=zh")
    assert r.status_code == 200
    assert 'class="diaghero"' in r.text  # HERO 诊断卡
    assert 'class="timeline"' in r.text  # 证据链 timeline
    assert r.text.count('class="tl-node') == 3  # 2 工具节点 + 1 末端结论菱形(末端含 tl-end)
    assert 'class="tl-node tl-end"' in r.text  # 终端结论节点
    assert "内存超限" in r.text  # 根因大字 + 结论回指
    assert "✓" in r.text and "✗" in r.text  # 成功/失败工具各一
    assert 'stroke-dasharray="92 8"' in r.text  # high → 92% 置信度环
    assert "docker restart postgres" in r.text  # 建议命令(永不自动执行)
    store.close()


async def test_hygiene_renders_tristate_and_nav(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)  # backup ok / disk·cert unevaluated
    store = Store(str(tmp_path / "s.db"))
    app = _build_app(store, settings)
    r = await _get(app, "/hygiene?lang=zh")
    assert r.status_code == 200
    assert 'class="hbanner"' in r.text  # 体检 banner
    assert 'class="hcard ok"' in r.text  # backup 健康卡
    assert 'class="hcard unevaluated"' in r.text  # disk/cert 未评估(灰态,不画绿)
    assert "未启用" in r.text  # hyg.unevaluated
    assert 'class="tab-on" title="体检"' in r.text  # 「体检」标签高亮
    store.close()


async def test_hygiene_alert_card_links_to_event(tmp_path, monkeypatch):
    settings = _settings(monkeypatch, SENTINEL_CERT_DOMAINS="a.com")
    store = Store(str(tmp_path / "s.db"))
    eid = store.insert_event(
        ts=1700000000,
        rule="cert_expiry",
        subject="a.com",
        severity="warning",
        status="open",
        detail="d",
        payload_json=json.dumps({"days_left": 5}),
        diagnosis_status="skipped",
        cooldown_until=0,
    )
    app = _build_app(store, settings)
    r = await _get(app, "/hygiene?lang=zh")
    assert r.status_code == 200
    assert 'class="hcard alert"' in r.text  # cert 告警卡
    assert f"/event/{eid}" in r.text  # 链到事件详情
    store.close()


async def test_hygiene_upstream_escaped_and_status_link(tmp_path, monkeypatch):
    from sentinel.models import (
        Component,
        ComponentStatus,
        Incident,
        IncidentStatus,
        Indicator,
        Snapshot,
    )

    settings = _settings(monkeypatch, SENTINEL_PROVIDERS="anthropic")
    store = Store(str(tmp_path / "s.db"))
    store.put(
        "anthropic",
        Snapshot(
            provider="anthropic",
            display_name="<x>Corp",
            indicator=Indicator.MINOR,
            status_url="https://status.example.com",
            components=[Component(key="api", name="API", status=ComponentStatus.OPERATIONAL)],
            incidents=[
                Incident(
                    key="bad",
                    title="poisoned link",
                    status=IncidentStatus.INVESTIGATING,
                    impact=Indicator.MINOR,
                    url="javascript:alert(1)",  # 投毒 shortlink
                    started_at=None,
                    updated_at=None,
                    latest_update_id="u1",
                )
            ],
            fetched_at="2026-06-16T00:00:00Z",
        ),
    )
    app = _build_app(store, settings)
    r = await _get(app, "/hygiene")
    assert r.status_code == 200
    assert "<x>Corp" not in r.text and "&lt;x&gt;Corp" in r.text  # 显示名转义
    assert "https://status.example.com" in r.text  # 状态页链接
    # 投毒 shortlink 不落进 href(scheme 白名单);精确查 href 而非裸 "javascript:"
    # ——后者会与版本模态里 changelog 正文「防 `javascript:` / `data:`」误撞。
    assert 'href="javascript:' not in r.text
    assert "alert(1)" not in r.text  # 投毒 payload 整体未泄漏
    assert "poisoned link" in r.text  # 标题仍展示(降级为纯文本,无链接)
    store.close()


async def test_hygiene_disabled_panel_404(tmp_path, monkeypatch):
    settings = _settings(monkeypatch, SENTINEL_PANEL_ENABLED="false")
    store = Store(str(tmp_path / "s.db"))
    app = _build_app(store, settings)
    r = await _get(app, "/hygiene")
    assert r.status_code == 404
    store.close()


# —— 导航重设计:版本胶囊 + 图标导航 + 更新提示 ——
async def test_nav_shows_version_pill_and_icon_nav(tmp_path, monkeypatch):
    from sentinel import __version__

    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    app = _build_app(store, settings)
    html = (await _get(app, "/")).text
    assert f"v{__version__}" in html  # 版本胶囊
    assert 'class="tabs"' in html  # 图标导航行
    assert "<svg" in html  # 内联 SVG 图标
    assert 'aria-label="中文"' in html and 'aria-label="English"' in html  # 语言控件可达名
    store.close()


async def test_nav_no_update_dot_when_no_meta(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    app = _build_app(store, settings)
    html = (await _get(app, "/")).text
    assert '<i class="updot">' not in html  # 无 meta → 不亮更新点元素
    store.close()


async def test_nav_shows_update_when_meta_set(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    store.set_meta("latest_known_version", "v999.0.0")
    store.set_meta("latest_release_url", "https://github.com/x/y/releases/v999.0.0")
    app = _build_app(store, settings)
    html = (await _get(app, "/")).text
    assert '<i class="updot">' in html  # 琥珀更新点元素
    assert "999.0.0" in html  # 更新命令含目标版本
    assert "https://github.com/x/y/releases/v999.0.0" in html  # release 链接
    # 版本胶囊 aria-label 在有更新时也播报"有新版"(屏幕阅读器不只听到当前版本)
    assert '· 新版 v999.0.0 可用"' in html
    store.close()


async def test_nav_sanitizes_poisoned_release_url(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    store.set_meta("latest_known_version", "v999.0.0")
    store.set_meta("latest_release_url", "javascript:alert(1)")
    app = _build_app(store, settings)
    html = (await _get(app, "/")).text
    assert "javascript:alert(1)" not in html  # _safe_http_url 抹空
    store.close()


async def test_nav_status_slim_refresh_and_neutral_ai(tmp_path, monkeypatch):
    # 行2 右侧只读态:刷新只留 ↻ HH:MM(完整日期/间隔进 title),LLM 改中性「AI 诊断 ✓」不泄露模型名
    import re

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
    app = _build_app(store, settings)
    app.state.llm_config = LLMConfig(settings)
    html = (await _get(app, "/")).text
    assert re.search(r"↻ \d{2}:\d{2}", html)  # 刷新只留时间
    assert "AI 诊断 ✓" in html  # 中性 AI 指示
    assert "deepseek-chat" not in html and "LLM:" not in html  # 模型名/旧前缀去除
    # 完整时间戳 + 间隔退到 title 悬停(不在可见正文堆叠)
    assert 'title="刷新于' in html
    store.close()


async def test_nav_no_ai_indicator_when_llm_off(tmp_path, monkeypatch):
    # LLM 未配置 → 顶栏不显示 AI 指示(不挂「未配置」噪音),只留刷新
    import re

    settings = _settings(monkeypatch)  # 无 llm.yaml / LLM_*
    store = Store(str(tmp_path / "s.db"))
    app = _build_app(store, settings)
    html = (await _get(app, "/")).text
    assert re.search(r"↻ \d{2}:\d{2}", html)
    # 仅查顶栏状态区(navrow 内 .sub mono span),不查整页——版本模态里的 changelog 正文含
    # 「AI 诊断」字样。注意 .sub mono 内嵌刷新子 span(↻ HH:MM),故须切到 navrow 的 </div>
    # 收口,而非第一个 </span>(否则会把其后的 AI 指示区切掉,断言变恒真)。
    nav_sub = html.split('class="sub mono">', 1)[1].split("</div>", 1)[0]
    assert "↻" in nav_sub  # 切片确含状态区(防再次切错成恒真)
    assert "AI 诊断" not in nav_sub  # 关态顶栏不显示 AI 行
    store.close()


async def test_nav_present_on_all_base_pages(tmp_path, monkeypatch):
    from sentinel import __version__

    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    app = _build_app(store, settings)
    for path in ("/services", "/events", "/hygiene"):
        html = (await _get(app, path)).text
        assert f"v{__version__}" in html, path
    store.close()


async def test_overview_glossary_uptime_tip(tmp_path, monkeypatch):
    # 今日可用率 caption 包成 hover 解释气泡(零-JS,环内 caption 用 pos=l)
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
    html = (await _get(app, "/")).text
    assert 'class="tip' in html
    assert 'role="tooltip"' in html
    assert 'id="tip-uptoday"' in html
    assert 'aria-describedby="tip-uptoday"' in html
    assert "探测成功的时间占比" in html  # gloss.uptime_today
    store.close()


async def test_overview_glossary_kpi_tips_unique_ids(tmp_path, monkeypatch):
    # 三个窗口均值气泡 id 必须互不相同(防 aria-describedby 撞 id);MTTR 亦有气泡
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
    html = (await _get(app, "/")).text
    for uid in ("tip-win7", "tip-win30", "tip-win90"):
        assert html.count(f'id="{uid}"') == 1, uid
    assert 'id="tip-mttr"' in html
    assert "每日可用率的平均值" in html  # gloss.win_mean
    assert "平均恢复时间" in html  # gloss.mttr
    store.close()


async def test_service_detail_glossary_tips(tmp_path, monkeypatch):
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
    html = (await _get(app, "/service/api?lang=zh")).text
    for uid in ("tip-p95", "tip-baseline", "tip-threshold"):
        assert f'id="{uid}"' in html, uid
    assert "95% 的请求响应快于此延迟" in html  # gloss.p95
    store.close()


async def test_event_detail_glossary_confidence_tip(tmp_path, monkeypatch):
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
        diagnosis_json=json.dumps({"summary": "OOM", "confidence": "high"}),
        tools_json=json.dumps([]),
    )
    app = _build_app(store, settings)
    html = (await _get(app, f"/event/{eid}")).text
    assert 'id="tip-conf"' in html
    assert 'class="tip l"' in html  # 环内 caption 用 pos=l(向右展开)
    assert "AI 诊断对该结论的置信度" in html  # gloss.confidence
    store.close()


async def test_footer_removed(tmp_path, monkeypatch):
    # 底部静态信任声明页脚已删(意义不大,且 localhost-only 在 LAN 暴露部署上不准)。
    # 仅查页脚元素本身不存在:旧页脚文案(localhost-only / never auto-run / 建议命令
    # 永不自动执行)现作为 changelog 历史条目正文出现在(display:none 的)版本模态里,
    # 故不再用整页 substring 反查文案(会误撞)。
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    app = _build_app(store, settings)
    html = (await _get(app, "/")).text
    assert 'class="footer' not in html
    html_en = (await _get(app, "/?lang=en")).text
    assert 'class="footer' not in html_en
    store.close()


# --- 事件 ⇄ 服务详情 双向导航打通(feat/event-service-nav-links) -----------------


def _ts_now(settings):
    from datetime import datetime, timedelta, timezone

    tz = timezone(timedelta(hours=settings.sentinel_heartbeat_utc_offset))
    return int(datetime.now(tz).timestamp())


def _real_service(store, name, now_ts):
    # 给某服务落今日探针样本 → 进 probe_daily∪今日样本 真实服务集
    from sentinel.models import ProbeSample

    store.add_probe_samples(
        [ProbeSample(ts=now_ts - 30, service=name, ok=True, status_code=200, latency_ms=12.0)]
    )


def _ev(store, *, ts, rule, subject, severity="critical", status="open"):
    store.insert_event(
        ts=ts,
        rule=rule,
        subject=subject,
        severity=severity,
        status=status,
        detail="d",
        payload_json="{}",
        diagnosis_status="skipped",
        cooldown_until=0,
    )


async def test_service_detail_links_to_events_filter(tmp_path, monkeypatch):
    # 服务详情「相关事件」给一条「全部相关事件 →」链到 /events?subject=<name>(同源筛选)。
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    now_ts = _ts_now(settings)
    _real_service(store, "api", now_ts)
    _ev(store, ts=now_ts - 60, rule="latency_degraded", subject="api", severity="warning")
    app = _build_app(store, settings)
    r = await _get(app, "/service/api?lang=zh")
    assert r.status_code == 200
    assert "/events?subject=api" in r.text  # 打通:相关事件→事件流(此服务)
    assert "全部相关事件" in r.text  # svc.all_events
    store.close()


async def test_service_detail_no_events_no_filter_link(tmp_path, monkeypatch):
    # 无相关事件时不显示「全部相关事件」链接(避免空筛选死链)。
    from sentinel.models import ProbeSample

    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    now_ts = _ts_now(settings)
    store.add_probe_samples(
        [ProbeSample(ts=now_ts - 30, service="api", ok=True, status_code=200, latency_ms=12.0)]
    )
    app = _build_app(store, settings)
    r = await _get(app, "/service/api?lang=zh")
    assert r.status_code == 200
    assert "/events?subject=api" not in r.text
    store.close()


async def test_events_page_links_real_service_only(tmp_path, monkeypatch):
    # 事件页服务名:真实探针服务可点回 /service/<name>;host 级 subject(disk "/")保持纯文本。
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    now_ts = _ts_now(settings)
    _real_service(store, "api", now_ts)
    _ev(store, ts=now_ts - 60, rule="service_down", subject="api")
    _ev(store, ts=now_ts - 50, rule="disk_usage", subject="/")  # host:无探针样本→非服务
    app = _build_app(store, settings)
    r = await _get(app, "/events?lang=zh")
    assert r.status_code == 200
    assert "/service/api?" in r.text  # 真实服务可点
    assert "/service/%2F" not in r.text  # host subject "/" 不可点
    store.close()


async def test_event_detail_subject_links_for_real_service(tmp_path, monkeypatch):
    # 事件详情:真实服务 subject 链回服务详情;非服务 subject 纯文本。
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    now_ts = _ts_now(settings)
    _real_service(store, "api", now_ts)
    _ev(store, ts=now_ts - 60, rule="service_down", subject="api")
    _ev(store, ts=now_ts - 50, rule="disk_usage", subject="/")
    app = _build_app(store, settings)
    rows = store.get_events_since(0)
    by_subj = {e.subject: e.id for e in rows}
    r_api = await _get(app, f"/event/{by_subj['api']}?lang=zh")
    assert r_api.status_code == 200
    assert "/service/api?" in r_api.text  # 真实服务可点
    r_disk = await _get(app, f"/event/{by_subj['/']}?lang=zh")
    assert r_disk.status_code == 200
    assert "/service/%2F" not in r_disk.text  # host subject 不可点
    store.close()


async def test_events_page_linkified_service_name_escaped(tmp_path, monkeypatch):
    # 可点服务名仍须转义:href 用 quote 百分号编码,显示文本 autoescape。
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    now_ts = _ts_now(settings)
    _real_service(store, "<x>svc", now_ts)
    _ev(store, ts=now_ts - 60, rule="service_down", subject="<x>svc")
    app = _build_app(store, settings)
    r = await _get(app, "/events?lang=zh")
    assert r.status_code == 200
    assert "<x>svc" not in r.text  # 未转义原串绝不出现
    assert "&lt;x&gt;svc" in r.text  # 显示文本转义
    assert "/service/%3Cx%3Esvc" in r.text  # href 路径百分号编码
    store.close()


async def test_event_detail_link_never_dead_for_stale_service(tmp_path, monkeypatch):
    # P2 回归:事件详情 subject 链接窗口必须与 /service 渲染窗口一致。
    # 陈旧服务(probe_daily 在 win 外)+ 老已恢复事件:旧实现按固定 90 天历史窗判可点,
    # 但链接携 win=30 → /service 在 30 天窗找不到数据 → 404 死链。修后 linkability 按同一
    # window_days 算:可点 ⟹ 该窗口下 /service 必渲染 200。
    from datetime import datetime, timedelta, timezone

    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    tz = timezone(timedelta(hours=settings.sentinel_heartbeat_utc_offset))
    today = datetime.now(tz).date()
    now_ts = int(datetime.now(tz).timestamp())
    # 60 天前逐日行(>默认 30 天窗、<90 天历史);120 天前已恢复事件(不在 30 天 feed、非 open)
    store.upsert_probe_daily(
        "api", (today - timedelta(days=60)).isoformat(), total=10, ok_count=10, p50=10.0, p95=20.0
    )
    store.insert_event(
        ts=now_ts - 120 * 86400,
        rule="service_down",
        subject="api",
        severity="critical",
        status="resolved",
        detail="d",
        payload_json="{}",
        diagnosis_status="skipped",
        cooldown_until=0,
    )
    eid = store.get_events_since(0)[0].id
    app = _build_app(store, settings)
    # 默认窗(30):陈旧服务不在 30 天 probe_daily 内、无 24h 样本 → 不可点(纯文本),无死链
    r30 = await _get(app, f"/event/{eid}?win=30")
    assert r30.status_code == 200
    assert "/service/api?" not in r30.text
    # win=90:可点;链接携 win=90,/service 必解析为 200(非 404 死链)
    r90 = await _get(app, f"/event/{eid}?win=90")
    assert "/service/api?" in r90.text
    assert (await _get(app, "/service/api?win=90")).status_code == 200
    store.close()


async def test_events_show_date_not_bare_time(tmp_path, monkeypatch):
    # bug:多日事件流里事件只显示 HH:MM,分不清是哪天(如 Dozzle 06-11 16:35 / 06-12 12:26
    # 被当成今天)。修后事件时间戳必须带「月-日」。
    from datetime import datetime, timedelta, timezone

    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    tz = timezone(timedelta(hours=settings.sentinel_heartbeat_utc_offset))
    dt = datetime.now(tz) - timedelta(days=5)  # 5 天前(同年)
    ts = int(dt.timestamp())
    store.insert_event(
        ts=ts,
        rule="container_restart",
        subject="dozzle",
        severity="warning",
        status="open",
        detail="d",
        payload_json="{}",
        diagnosis_status="skipped",
        cooldown_until=0,
    )
    app = _build_app(store, settings)
    r = await _get(app, "/events?lang=zh")
    assert r.status_code == 200
    expected = datetime.fromtimestamp(ts, tz).strftime("%m-%d %H:%M")  # 月-日 时:分
    assert expected in r.text  # 带日期,不再是裸 HH:MM
    store.close()


def test_when_formatter_includes_date_and_year_when_crossyear(monkeypatch):
    # 单元:同年带「月-日 时:分」,跨年带「年-月-日 时:分」。
    from datetime import datetime, timedelta, timezone

    from sentinel.panel import view

    tz = timezone(timedelta(hours=8))
    now = datetime.now(tz)
    same_year = now.replace(month=1, day=2, hour=3, minute=4)
    assert view._when(int(same_year.timestamp()), tz) == same_year.strftime("%m-%d %H:%M")
    old = now - timedelta(days=400)  # 必跨年
    assert view._when(int(old.timestamp()), tz) == old.strftime("%Y-%m-%d %H:%M")
