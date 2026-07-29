# tests/test_store.py
from sentinel.models import Indicator, ProbeSample, Snapshot
from sentinel.store import Store


def _snap(provider: str, indicator: Indicator) -> Snapshot:
    return Snapshot(
        provider=provider,
        display_name=provider,
        indicator=indicator,
        status_url="https://x",
        components=[],
        incidents=[],
        fetched_at="2026-06-06T00:00:00Z",
    )


def test_get_missing_returns_none(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    assert store.get("anthropic") is None


def test_put_then_get_roundtrips(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.put("anthropic", _snap("anthropic", Indicator.MAJOR))
    got = store.get("anthropic")
    assert got is not None
    assert got.indicator is Indicator.MAJOR


def test_put_overwrites_previous(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.put("anthropic", _snap("anthropic", Indicator.NONE))
    store.put("anthropic", _snap("anthropic", Indicator.CRITICAL))
    assert store.get("anthropic").indicator is Indicator.CRITICAL


def test_survives_reopen(tmp_path):
    path = str(tmp_path / "s.db")
    Store(path).put("openai", _snap("openai", Indicator.MINOR))
    assert Store(path).get("openai").indicator is Indicator.MINOR


def test_meta_missing_returns_none(tmp_path):
    assert Store(str(tmp_path / "s.db")).get_meta("heartbeat_last_date") is None


def test_meta_set_get_and_overwrite(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.set_meta("heartbeat_last_date", "2026-06-06")
    assert store.get_meta("heartbeat_last_date") == "2026-06-06"
    store.set_meta("heartbeat_last_date", "2026-06-07")
    assert store.get_meta("heartbeat_last_date") == "2026-06-07"


def test_meta_survives_reopen(tmp_path):
    path = str(tmp_path / "s.db")
    Store(path).set_meta("k", "v")
    assert Store(path).get_meta("k") == "v"


# ---- 内部探针表 ----


def _sample(ts, service="auth", ok=True, status_code=200, latency_ms=12.5):
    return ProbeSample(
        ts=ts, service=service, ok=ok, status_code=status_code, latency_ms=latency_ms
    )


def test_wal_mode_enabled(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    assert store._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_probe_samples_roundtrip_and_since_filter(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.add_probe_samples(
        [_sample(100), _sample(200, ok=False, status_code=None, latency_ms=None)]
    )
    got = store.get_probe_samples_since(150)
    assert len(got) == 1
    assert got[0].ts == 200
    assert got[0].ok is False
    assert got[0].status_code is None
    assert got[0].latency_ms is None


def test_probe_samples_survive_reopen(tmp_path):
    path = str(tmp_path / "s.db")
    Store(path).add_probe_samples([_sample(100)])
    assert len(Store(path).get_probe_samples_since(0)) == 1


def test_prune_probe_samples(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.add_probe_samples([_sample(100), _sample(200), _sample(300)])
    deleted = store.prune_probe_samples(250)
    assert deleted == 2
    assert [s.ts for s in store.get_probe_samples_since(0)] == [300]


def test_upsert_probe_daily_overwrites(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.upsert_probe_daily("auth", "2026-06-11", total=288, ok_count=288, p50=10.0, p95=20.0)
    store.upsert_probe_daily("auth", "2026-06-11", total=290, ok_count=289, p50=11.0, p95=22.0)
    p95s = store.get_recent_daily_p95s("auth", before_date="2026-06-12")
    assert p95s == [22.0]


def test_get_recent_daily_p95s_order_limit_and_null(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    for i in range(1, 10):  # 06-01 .. 06-09
        store.upsert_probe_daily(
            "auth", f"2026-06-0{i}", total=10, ok_count=10, p50=1.0, p95=float(i)
        )
    store.upsert_probe_daily("auth", "2026-06-10", total=0, ok_count=0, p50=None, p95=None)
    p95s = store.get_recent_daily_p95s("auth", before_date="2026-06-11", limit=7)
    # 最近优先、NULL 被过滤、不含 before_date 当天及之后、最多 7 条
    assert p95s == [9.0, 8.0, 7.0, 6.0, 5.0, 4.0, 3.0]


def test_events_lifecycle(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    eid = store.insert_event(
        ts=1000,
        rule="service_down",
        subject="auth",
        severity="critical",
        status="open",
        detail="连续 3 次失败",
        payload_json="{}",
        diagnosis_status="pending",
        cooldown_until=1000 + 21600,
    )
    assert eid > 0
    opens = store.get_open_events()
    assert [(e.rule, e.subject, e.status) for e in opens] == [("service_down", "auth", "open")]
    assert opens[0].diagnosis_status == "pending"
    assert opens[0].resolved_ts is None
    assert store.get_pending_diagnosis_events()[0].id == eid

    store.resolve_event(eid, resolved_ts=2000)
    assert store.get_open_events() == []
    assert store.count_resolved_since(1500) == 1
    assert store.count_resolved_since(2000) == 1  # >= 闭边界
    assert store.count_resolved_since(2500) == 0
    # resolve 不动 diagnosis_status:Phase 3 仍能拉到已恢复但未诊断的事件
    assert store.get_pending_diagnosis_events()[0].id == eid
    store.close()


def test_event_cooldown_lookup(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    assert store.get_cooldown_until("disk_usage", "/") == 0
    store.insert_event(
        ts=1000,
        rule="disk_usage",
        subject="/",
        severity="critical",
        status="resolved",
        detail="d",
        payload_json="{}",
        diagnosis_status="skipped",
        cooldown_until=5000,
        resolved_ts=1500,
    )
    store.insert_event(
        ts=2000,
        rule="disk_usage",
        subject="/",
        severity="critical",
        status="resolved",
        detail="d",
        payload_json="{}",
        diagnosis_status="skipped",
        cooldown_until=9000,
        resolved_ts=2500,
    )
    assert store.get_cooldown_until("disk_usage", "/") == 9000  # 取历史最大
    assert store.get_cooldown_until("disk_usage", "/other") == 0  # subject 隔离
    store.close()


def test_rule_sets_cover_rule_names():
    from sentinel import findings

    covered = (
        findings.PROBE_RULES
        | findings.METRICS_RULES
        | findings.LOG_RULES
        | findings.ERROR_RULES
        | findings.HYGIENE_RULES
        | findings.DOCKER_RULES
        | {"scan_failed_prometheus", "scan_failed_loki", "scan_failed_docker"}
    )
    assert covered == findings.RULE_NAMES.keys()


def test_ensure_column_idempotent_migration(tmp_path):
    import sqlite3

    from sentinel.store import _ensure_column

    conn = sqlite3.connect(str(tmp_path / "old.db"))
    conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, rule TEXT)")
    _ensure_column(conn, "events", "diagnosis_tools_json", "TEXT")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()]
    assert "diagnosis_tools_json" in cols
    # 重复执行无副作用（不重复加列、不抛）
    _ensure_column(conn, "events", "diagnosis_tools_json", "TEXT")
    cols2 = [r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()]
    assert cols2.count("diagnosis_tools_json") == 1
    conn.close()


def test_store_migrates_legacy_events_with_notified_default(tmp_path):
    import sqlite3

    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, "
        "rule TEXT NOT NULL, subject TEXT NOT NULL, severity TEXT NOT NULL, "
        "status TEXT NOT NULL, detail TEXT NOT NULL, payload_json TEXT NOT NULL, "
        "diagnosis_status TEXT NOT NULL, diagnosis_json TEXT, "
        "cooldown_until INTEGER NOT NULL, resolved_ts INTEGER, "
        "diagnosis_tools_json TEXT)"
    )
    conn.execute(
        "INSERT INTO events "
        "(ts, rule, subject, severity, status, detail, payload_json, "
        "diagnosis_status, cooldown_until) "
        "VALUES (1000, 'service_down', 'auth', 'critical', 'open', "
        "'失败', '{}', 'pending', 0)"
    )
    conn.commit()
    conn.close()

    store = Store(path)
    assert store.get_open_events()[0].notified is True
    store.close()


def test_set_diagnosis_tools_json_roundtrip(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    eid = store.insert_event(
        ts=1000,
        rule="container_down",
        subject="pg",
        severity="critical",
        status="open",
        detail="d",
        payload_json="{}",
        diagnosis_status="pending",
        cooldown_until=0,
    )
    tools = '[{"tool": "docker_inspect", "args": {"name": "pg"}, "output": "{}", "ok": true}]'
    store.set_diagnosis(eid, status="done", diagnosis_json='{"summary": "s"}', tools_json=tools)
    e = store.get_event(eid)
    assert e.diagnosis_tools_json == tools
    assert e.diagnosis_status == "done"


def test_set_diagnosis_defaults_tools_json_none(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    eid = store.insert_event(
        ts=1000,
        rule="disk_usage",
        subject="/",
        severity="warning",
        status="open",
        detail="d",
        payload_json="{}",
        diagnosis_status="pending",
        cooldown_until=0,
    )
    store.set_diagnosis(eid, status="skipped")  # 不传 tools_json → NULL
    assert store.get_event(eid).diagnosis_tools_json is None


def test_get_resolved_since_orders_and_limits(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    for ts in (1000, 2000, 3000):
        rid = store.insert_event(
            ts=ts,
            rule="disk_usage",
            subject=f"/{ts}",
            severity="warning",
            status="open",
            detail="d",
            payload_json="{}",
            diagnosis_status="skipped",
            cooldown_until=0,
        )
        store.resolve_event(rid, resolved_ts=ts + 100)
    got = store.get_resolved_since(0)
    assert [e.resolved_ts for e in got] == [3100, 2100, 1100]  # 最近在前
    assert [e.subject for e in store.get_resolved_since(2500)] == ["/3000"]  # >= 闭边界
    assert len(store.get_resolved_since(0, limit=2)) == 2  # limit 生效
    store.close()


def test_restart_baseline_roundtrip(tmp_path):
    from sentinel.store import Store

    store = Store(str(tmp_path / "s.db"))
    assert store.get_restart_baseline("web") is None
    store.upsert_restart_baseline("web", 3, 1000)
    assert store.get_restart_baseline("web") == (3, 1000)
    # ON CONFLICT(subject) DO UPDATE: second upsert overwrites
    store.upsert_restart_baseline("web", 7, 2000)
    assert store.get_restart_baseline("web") == (7, 2000)
    store.close()


def test_prune_restart_baselines(tmp_path):
    from sentinel.store import Store

    store = Store(str(tmp_path / "s.db"))
    store.upsert_restart_baseline("old", 1, 1000)
    store.upsert_restart_baseline("new", 1, 5000)
    store.prune_restart_baselines(older_than_ts=3000)  # window_start < 3000 deleted
    assert store.get_restart_baseline("old") is None
    assert store.get_restart_baseline("new") == (1, 5000)
    store.close()


def test_prune_restart_baselines_boundary_keeps_equal(tmp_path):
    from sentinel.store import Store

    store = Store(str(tmp_path / "s.db"))
    store.upsert_restart_baseline("edge", 1, 3000)
    store.prune_restart_baselines(older_than_ts=3000)  # strict < : 3000 is NOT deleted
    assert store.get_restart_baseline("edge") == (1, 3000)
    store.close()
