from datetime import datetime, timedelta, timezone

from sentinel.config import Settings
from sentinel.models import ProbeSample
from sentinel.store import Store

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 6, 14, 14, 0, tzinfo=TZ)
NOW_TS = int(NOW.timestamp())
MIDNIGHT_TS = int(datetime(2026, 6, 14, 0, 0, tzinfo=TZ).timestamp())


def _settings(monkeypatch, **env):
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)


def test_day_state_thresholds(monkeypatch):
    from sentinel.panel.view import _day_state

    s = _settings(monkeypatch)  # red=50.0, partial=99.5
    assert _day_state(None, frozenset(), s) == "nodata"
    assert _day_state(100.0, frozenset(), s) == "ok"
    assert _day_state(99.5, frozenset(), s) == "ok"  # 边界含
    assert _day_state(99.4, frozenset(), s) == "partial"
    assert _day_state(60.0, frozenset(), s) == "partial"
    assert _day_state(50.0, frozenset(), s) == "partial"  # 50 不 < 50 → 非 down
    assert _day_state(49.9, frozenset(), s) == "down"
    assert _day_state(100.0, {"latency_degraded"}, s) == "degraded"
    assert _day_state(100.0, {"mem_pressure"}, s) == "degraded"
    assert _day_state(100.0, {"container_unhealthy"}, s) == "partial"
    assert _day_state(100.0, {"service_down"}, s) == "down"
    assert _day_state(100.0, {"container_down"}, s) == "down"
    # 优先级：nodata 压一切；down 压 degraded
    assert _day_state(None, {"service_down"}, s) == "nodata"
    assert _day_state(100.0, {"service_down", "latency_degraded"}, s) == "down"


def test_service_health_bars_history_today_nodata(tmp_path, monkeypatch):
    from sentinel.panel.view import _service_health_bars

    s = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    d3 = (NOW - timedelta(days=3)).date().isoformat()  # 06-11
    d2 = (NOW - timedelta(days=2)).date().isoformat()  # 06-12
    d1 = (NOW - timedelta(days=1)).date().isoformat()  # 06-13 (无 daily → nodata)
    today = NOW.date().isoformat()  # 06-14
    store.upsert_probe_daily("api", d3, total=100, ok_count=100, p50=50.0, p95=80.0)  # ok
    store.upsert_probe_daily("api", d2, total=100, ok_count=40, p50=50.0, p95=80.0)  # 40% → down
    # 今天实时：1 ok / 1 fail → 50% → partial
    store.add_probe_samples(
        [
            ProbeSample(ts=NOW_TS - 100, service="api", ok=True, status_code=200, latency_ms=120.0),
            ProbeSample(ts=NOW_TS - 50, service="api", ok=False, status_code=500, latency_ms=None),
        ]
    )
    today_samples = store.get_probe_samples_since(MIDNIGHT_TS)
    bars = _service_health_bars(
        store, s, now_ts=NOW_TS, tz=TZ, window_days=4, today_samples=today_samples
    )
    api = {b["service"]: b for b in bars}["api"]
    assert len(api["days"]) == 4  # 06-11..06-14
    by_date = {d["date"]: d for d in api["days"]}
    assert by_date[d3]["state"] == "ok"
    assert by_date[d2]["state"] == "down"
    assert by_date[d1]["state"] == "nodata"
    assert by_date[today]["state"] == "partial"
    assert by_date[today]["is_today"] is True
    assert by_date[d3]["is_today"] is False
    assert api["uptime_pct"] == 50.0  # 今天 service 级汇总
    store.close()


def test_service_health_bars_event_colors_only_matching_subject(tmp_path, monkeypatch):
    from sentinel.panel.view import _service_health_bars

    s = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    d2_date = (NOW - timedelta(days=2)).date()
    d2 = d2_date.isoformat()
    store.upsert_probe_daily("api", d2, total=100, ok_count=100, p50=50.0, p95=80.0)  # uptime ok
    ts_d2 = int(datetime(d2_date.year, d2_date.month, d2_date.day, 12, tzinfo=TZ).timestamp())
    # 服务 subject 命中 → 把 ok 升级到 degraded
    store.insert_event(
        ts=ts_d2,
        rule="latency_degraded",
        subject="api",
        severity="warning",
        status="open",
        detail="d",
        payload_json="{}",
        diagnosis_status="skipped",
        cooldown_until=0,
    )
    # 非服务 subject → 不染任何服务格子，也不产生新服务行
    store.insert_event(
        ts=ts_d2,
        rule="service_down",
        subject="some-container",
        severity="critical",
        status="open",
        detail="d",
        payload_json="{}",
        diagnosis_status="pending",
        cooldown_until=0,
    )
    today_samples = store.get_probe_samples_since(MIDNIGHT_TS)
    bars = _service_health_bars(
        store, s, now_ts=NOW_TS, tz=TZ, window_days=4, today_samples=today_samples
    )
    services = {b["service"] for b in bars}
    assert "api" in services and "some-container" not in services
    api = {b["service"]: b for b in bars}["api"]
    assert {d["date"]: d for d in api["days"]}[d2]["state"] == "degraded"
    store.close()


def test_service_health_bars_empty_store(tmp_path, monkeypatch):
    from sentinel.panel.view import _service_health_bars

    s = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    bars = _service_health_bars(store, s, now_ts=NOW_TS, tz=TZ, window_days=30, today_samples=[])
    assert bars == []
    store.close()


def test_host_self_engine_liveness(monkeypatch):
    from sentinel.panel.view import _host_self

    s = _settings(monkeypatch)  # sentinel_probe_interval 默认 300 → 阈值 600s
    llm_off = {"enabled": False, "pending_restart": False, "model": None}
    live = _host_self(s, latest_probe_ts=NOW_TS - 100, now_ts=NOW_TS, llm_config=None, llm=llm_off)
    assert live["probe_engine_live"] is True
    assert live["llm"] == {"active": None, "fallback": None, "fallback_ready": False}
    stale = _host_self(s, latest_probe_ts=NOW_TS - 700, now_ts=NOW_TS, llm_config=None, llm=llm_off)
    assert stale["probe_engine_live"] is False
    none = _host_self(s, latest_probe_ts=None, now_ts=NOW_TS, llm_config=None, llm=llm_off)
    assert none["probe_engine_live"] is None  # 待确认


def test_host_self_llm_from_env_posture(monkeypatch):
    from sentinel.panel.view import _host_self

    s = _settings(monkeypatch)
    llm = {"enabled": True, "pending_restart": False, "model": "deepseek-chat"}
    hs = _host_self(s, latest_probe_ts=NOW_TS, now_ts=NOW_TS, llm_config=None, llm=llm)
    # env 路径无 fallback 概念
    assert hs["llm"] == {"active": "deepseek-chat", "fallback": None, "fallback_ready": False}


def test_host_self_llm_active_fallback_from_config(tmp_path, monkeypatch):
    from sentinel.llm_config import LLMConfig
    from sentinel.panel.view import _host_self

    path = tmp_path / "llm.yaml"
    path.write_text(
        "active: deepseek\nfallback: kimi\nproviders:\n"
        "  deepseek:\n    base_url: https://api.deepseek.com/v1\n"
        "    model: deepseek-chat\n    api_key: ''\n"
        "  kimi:\n    base_url: https://api.moonshot.cn/v1\n"
        "    model: kimi-k2\n    api_key: ''\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv("SENTINEL_LLM_CONFIG_FILE", str(path))
    s = Settings(_env_file=None)
    cfg = LLMConfig(s)
    hs = _host_self(
        s,
        latest_probe_ts=NOW_TS,
        now_ts=NOW_TS,
        llm_config=cfg,
        llm={"enabled": True, "pending_restart": False, "model": "deepseek-chat"},
    )
    assert hs["llm"] == {"active": "deepseek-chat", "fallback": "kimi-k2", "fallback_ready": True}
