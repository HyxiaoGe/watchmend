# tests/test_report.py
from datetime import datetime, timedelta, timezone

from sentinel.models import ProbeSample
from sentinel.report import aggregate_window, build_daily_stats, percentile, run_daily_report
from sentinel.store import Store

TZ = timezone(timedelta(hours=8))


class RecordingFeishu:
    def __init__(self, fail=False):
        self.fail = fail
        self.sent = []

    async def send(self, card):
        self.sent.append(card)
        if self.fail:
            from sentinel.feishu.client import FeishuError

            raise FeishuError("boom")


def _sample(ts, service="auth", ok=True, latency_ms=10.0):
    return ProbeSample(
        ts=ts,
        service=service,
        ok=ok,
        status_code=200 if ok else None,
        latency_ms=latency_ms if ok else None,
    )


# ---- percentile ----


def test_percentile_empty_returns_none():
    assert percentile([], 95) is None


def test_percentile_nearest_rank():
    values = [float(i) for i in range(1, 101)]  # 1..100
    assert percentile(values, 50) == 50.0
    assert percentile(values, 95) == 95.0
    assert percentile([42.0], 95) == 42.0


# ---- aggregate_window ----


def test_aggregate_includes_zero_sample_services():
    stats = aggregate_window([], ["auth", "fusion"])
    assert {s.service for s in stats} == {"auth", "fusion"}
    assert all(s.total == 0 and s.uptime_pct == 0.0 for s in stats)


def test_aggregate_latency_only_from_ok_samples():
    samples = [
        _sample(1, latency_ms=10.0),
        _sample(2, latency_ms=20.0),
        _sample(3, ok=False),  # 失败样本不进延迟统计
    ]
    (auth,) = aggregate_window(samples, ["auth"])
    assert auth.total == 3
    assert auth.ok_count == 2
    assert auth.p50_ms == 10.0
    assert auth.p95_ms == 20.0


def test_aggregate_keeps_services_not_in_config():
    # 窗口内还有已下线服务的样本 → 照样聚合展示,不丢数据
    stats = aggregate_window([_sample(1, service="legacy")], ["auth"])
    assert {s.service for s in stats} == {"auth", "legacy"}


# ---- build_daily_stats(基线) ----


def test_build_daily_stats_baseline_mean_of_recent_p95(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    now = datetime(2026, 6, 12, 9, 0, tzinfo=TZ)
    now_ts = int(now.timestamp())
    store.add_probe_samples([_sample(now_ts - 60)])
    store.upsert_probe_daily("auth", "2026-06-10", total=288, ok_count=288, p50=8.0, p95=16.0)
    store.upsert_probe_daily("auth", "2026-06-11", total=288, ok_count=288, p50=9.0, p95=24.0)
    (auth,) = build_daily_stats(store, ["auth"], now_ts=now_ts, date_str="2026-06-12")
    assert auth.baseline_p95_ms == 20.0  # (16+24)/2
    (fresh,) = aggregate_window([], ["x"])
    assert fresh.baseline_p95_ms is None


# ---- run_daily_report ----


async def test_run_daily_report_skips_before_hour(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    feishu = RecordingFeishu()
    now = datetime(2026, 6, 12, 8, 59, tzinfo=TZ)
    sent = await run_daily_report(
        store=store,
        feishu=feishu,
        services=["auth"],
        now_local=now,
        hour=9,
        retention_days=30,
    )
    assert sent is False
    assert feishu.sent == []


async def test_run_daily_report_sends_then_commits(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    now = datetime(2026, 6, 12, 9, 0, tzinfo=TZ)
    now_ts = int(now.timestamp())
    store.add_probe_samples(
        [_sample(now_ts - 60), _sample(now_ts - 40 * 86400, service="auth")]  # 一条过期样本
    )
    feishu = RecordingFeishu()
    sent = await run_daily_report(
        store=store,
        feishu=feishu,
        services=["auth"],
        now_local=now,
        hour=9,
        retention_days=30,
    )
    assert sent is True
    assert len(feishu.sent) == 1
    assert store.get_meta("daily_report_last_date") == "2026-06-12"
    assert store.get_recent_daily_p95s("auth", before_date="2026-06-13") == [10.0]
    # 30 天前的样本已清理
    assert len(store.get_probe_samples_since(0)) == 1
    # 同日再调不重发
    again = await run_daily_report(
        store=store,
        feishu=feishu,
        services=["auth"],
        now_local=now.replace(hour=11),
        hour=9,
        retention_days=30,
    )
    assert again is False
    assert len(feishu.sent) == 1


async def test_run_daily_report_send_failure_commits_nothing(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    now = datetime(2026, 6, 12, 9, 0, tzinfo=TZ)
    store.add_probe_samples([_sample(int(now.timestamp()) - 60)])
    feishu = RecordingFeishu(fail=True)
    try:
        await run_daily_report(
            store=store,
            feishu=feishu,
            services=["auth"],
            now_local=now,
            hour=9,
            retention_days=30,
        )
    except Exception:
        pass  # FeishuError 向上抛由 Job 循环记日志
    assert store.get_meta("daily_report_last_date") is None  # 未 commit → 下一分钟重试
    assert store.get_recent_daily_p95s("auth", before_date="2026-06-13") == []


async def test_daily_report_card_includes_open_events(tmp_path):
    # 复用本文件现有的 fake feishu / store 构造方式;关键断言:卡片带未决事件区
    from datetime import datetime, timedelta, timezone

    from sentinel.models import ProbeSample
    from sentinel.report import run_daily_report
    from sentinel.store import Store

    class FakeFeishu:
        def __init__(self):
            self.cards = []

        async def send(self, card):
            self.cards.append(card)

    store = Store(str(tmp_path / "r.db"))
    now_local = datetime(2026, 6, 11, 9, 30, tzinfo=timezone(timedelta(hours=8)))
    now_ts = int(now_local.timestamp())
    store.add_probe_samples(
        [ProbeSample(ts=now_ts - 600, service="auth", ok=True, status_code=200, latency_ms=30.0)]
    )
    store.insert_event(
        ts=now_ts - 3600,
        rule="mem_pressure",
        subject="swap",
        severity="warning",
        status="open",
        detail="swap 85%",
        payload_json="{}",
        diagnosis_status="pending",
        cooldown_until=now_ts + 21600,
    )
    feishu = FakeFeishu()
    sent = await run_daily_report(
        store=store,
        feishu=feishu,
        services=["auth"],
        now_local=now_local,
        hour=9,
        retention_days=30,
    )
    assert sent
    contents = [
        e["text"]["content"] for e in feishu.cards[0]["card"]["elements"] if e.get("tag") == "div"
    ]
    assert any("未决事件 1 起" in c for c in contents)
    assert any("内存压力 · swap" in c for c in contents)
    store.close()


def test_report_due_gate(tmp_path):
    from datetime import datetime, timedelta, timezone

    from sentinel.report import report_due
    from sentinel.store import Store

    store = Store(str(tmp_path / "g.db"))
    tz = timezone(timedelta(hours=8))
    at_nine = datetime(2026, 6, 11, 9, 5, tzinfo=tz)
    assert report_due(store, at_nine, 9)
    store.set_meta("daily_report_last_date", "2026-06-11")
    assert not report_due(store, at_nine, 9)  # 当天已发
    assert not report_due(store, datetime(2026, 6, 12, 8, 0, tzinfo=tz), 9)  # 没到点
    store.close()
