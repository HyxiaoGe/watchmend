from sentinel.models import ProbeDailyRow
from sentinel.store import Store


def test_get_probe_daily_since_range_and_order(tmp_path):
    s = Store(str(tmp_path / "s.db"))
    s.upsert_probe_daily("api", "2026-06-10", total=10, ok_count=10, p50=100.0, p95=200.0)
    s.upsert_probe_daily("api", "2026-06-12", total=8, ok_count=7, p50=110.0, p95=None)
    s.upsert_probe_daily("api", "2026-06-08", total=5, ok_count=5, p50=90.0, p95=150.0)
    rows = s.get_probe_daily_since("2026-06-10")
    assert [r.date for r in rows] == ["2026-06-10", "2026-06-12"]  # 06-08 排除，升序
    assert isinstance(rows[0], ProbeDailyRow)
    assert rows[0].service == "api" and rows[0].total == 10 and rows[0].p95_ms == 200.0
    assert rows[1].p95_ms is None and rows[1].ok_count == 7
    s.close()


def test_get_probe_daily_since_empty(tmp_path):
    s = Store(str(tmp_path / "s.db"))
    assert s.get_probe_daily_since("2026-01-01") == []
    s.close()


def test_get_events_since_open_and_resolved_desc(tmp_path):
    s = Store(str(tmp_path / "s.db"))
    o = s.insert_event(
        ts=1000,
        rule="service_down",
        subject="api",
        severity="critical",
        status="open",
        detail="d",
        payload_json="{}",
        diagnosis_status="pending",
        cooldown_until=0,
    )
    r = s.insert_event(
        ts=2000,
        rule="latency_degraded",
        subject="api",
        severity="warning",
        status="open",
        detail="d",
        payload_json="{}",
        diagnosis_status="skipped",
        cooldown_until=0,
    )
    s.resolve_event(r, resolved_ts=2500)
    old = s.insert_event(
        ts=10,
        rule="disk_usage",
        subject="/",
        severity="warning",
        status="open",
        detail="d",
        payload_json="{}",
        diagnosis_status="skipped",
        cooldown_until=0,
    )
    evs = s.get_events_since(1000)
    ids = [e.id for e in evs]
    assert old not in ids  # ts < since 被排除
    assert ids == [r, o]  # ts 降序；open 与 resolved 都纳入
    assert s.get_events_since(1000, limit=1)[0].id == r  # limit 生效
    s.close()
