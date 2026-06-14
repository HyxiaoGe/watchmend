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
    assert rules["container_down"]["rule_label"] == "容器停止"
    assert rules["scan_failed_loki"]["is_scan_failure"] is True
    assert rules["scan_failed_loki"]["lifecycle"] == "scan_failed"
    p = ov["posture"]
    assert p["layers"] == {"prometheus": True, "loki": True, "docker": False, "llm": True}
    assert p["llm"] == {"enabled": True, "model": "deepseek-chat"}
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
    assert ov["posture"]["llm"] == {"enabled": False, "model": None}
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
    assert ov["posture"]["llm"] == {"enabled": True, "model": "deepseek-chat"}
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
