# tests/test_probe_rules.py
import pytest

from sentinel.models import ProbeSample
from sentinel.probe_rules import evaluate_probe_rules
from sentinel.store import Store

NOW = 1_760_000_000
DATE = "2026-06-11"


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    from sentinel.config import Settings

    return Settings(_env_file=None)


def _store(tmp_path):
    return Store(str(tmp_path / "p.db"))


def _add(store, service, specs):
    """specs: list[(offset_s, ok, latency_ms)],offset 相对 NOW 往回。"""
    store.add_probe_samples(
        [
            ProbeSample(
                ts=NOW - off,
                service=service,
                ok=ok,
                status_code=200 if ok else 502,
                latency_ms=lat,
            )
            for off, ok, lat in specs
        ]
    )


def test_service_down_after_streak(tmp_path, settings):
    store = _store(tmp_path)
    _add(store, "auth", [(900, False, None), (600, False, None), (300, False, None)])
    findings, hold = evaluate_probe_rules(store, ["auth"], settings, now_ts=NOW, date_str=DATE)
    assert [(f.rule, f.subject, f.severity) for f in findings] == [
        ("service_down", "auth", "critical")
    ]
    assert findings[0].needs_diagnosis
    assert "连续 3 次" in findings[0].detail
    assert ("latency_degraded", "auth") in hold  # 挂掉期间延迟评估被跳过,防误判恢复
    store.close()


def test_no_service_down_when_latest_ok(tmp_path, settings):
    store = _store(tmp_path)
    _add(store, "auth", [(900, False, None), (600, False, None), (300, True, 30.0)])
    findings, _ = evaluate_probe_rules(store, ["auth"], settings, now_ts=NOW, date_str=DATE)
    assert findings == []
    store.close()


def test_service_down_ignores_stale_failures_beyond_window(tmp_path, settings):
    store = _store(tmp_path)
    # 停机跨越的陈旧失败样本(>1h 前)不参与连败判定:重启后首败不该立刻拼出"连续 3 次"
    _add(store, "auth", [(7200, False, None), (5400, False, None), (300, False, None)])
    findings, hold = evaluate_probe_rules(store, ["auth"], settings, now_ts=NOW, date_str=DATE)
    assert findings == []
    assert ("latency_degraded", "auth") in hold  # 窗口内 ok 样本不足,延迟评估也被跳过
    store.close()


def test_latency_degraded_against_baseline(tmp_path, settings):
    store = _store(tmp_path)
    for d in ("2026-06-08", "2026-06-09", "2026-06-10"):
        store.upsert_probe_daily("auth", d, total=288, ok_count=288, p50=80.0, p95=100.0)
    _add(store, "auth", [(i * 300, True, 700.0) for i in range(1, 8)])  # 7 个 ok 样本
    findings, hold = evaluate_probe_rules(store, ["auth"], settings, now_ts=NOW, date_str=DATE)
    assert [(f.rule, f.subject) for f in findings] == [("latency_degraded", "auth")]
    assert "基线 100" in findings[0].detail
    assert findings[0].payload["p95_1h_ms"] == 700.0
    assert hold == set()  # 正常评估,无豁免
    store.close()


def test_latency_needs_min_samples_and_holds(tmp_path, settings):
    store = _store(tmp_path)
    store.upsert_probe_daily("auth", "2026-06-10", total=288, ok_count=288, p50=80.0, p95=100.0)
    _add(store, "auth", [(i * 300, True, 700.0) for i in range(1, 6)])  # 只有 5 个
    findings, hold = evaluate_probe_rules(store, ["auth"], settings, now_ts=NOW, date_str=DATE)
    assert findings == []
    assert hold == {("latency_degraded", "auth")}
    store.close()


def test_latency_skipped_without_baseline_no_hold(tmp_path, settings):
    store = _store(tmp_path)
    _add(store, "auth", [(i * 300, True, 700.0) for i in range(1, 8)])
    findings, hold = evaluate_probe_rules(store, ["auth"], settings, now_ts=NOW, date_str=DATE)
    assert findings == []
    assert hold == set()  # 从未有过基线就不可能有 open 延迟事件,无需豁免
    store.close()


def test_down_service_skips_latency_rule(tmp_path, settings):
    store = _store(tmp_path)
    store.upsert_probe_daily("auth", "2026-06-10", total=288, ok_count=288, p50=80.0, p95=100.0)
    _add(store, "auth", [(i * 300, True, 900.0) for i in range(4, 10)])  # 早些时候很慢
    _add(store, "auth", [(900, False, None), (600, False, None), (300, False, None)])
    findings, hold = evaluate_probe_rules(store, ["auth"], settings, now_ts=NOW, date_str=DATE)
    assert [f.rule for f in findings] == ["service_down"]  # 服务已挂,不再叠延迟卡
    assert ("latency_degraded", "auth") in hold
    store.close()


def test_partial_failures_hold_service_down_no_false_recovery(tmp_path, settings):
    store = _store(tmp_path)
    # sentinel 自身停机后重启:窗口内只剩 1 条失败,连败不满但无恢复证据
    _add(store, "auth", [(300, False, None)])
    findings, hold = evaluate_probe_rules(store, ["auth"], settings, now_ts=NOW, date_str=DATE)
    assert findings == []
    assert ("service_down", "auth") in hold
    store.close()


def test_no_samples_hold_service_down(tmp_path, settings):
    store = _store(tmp_path)
    findings, hold = evaluate_probe_rules(store, ["auth"], settings, now_ts=NOW, date_str=DATE)
    assert findings == []
    assert ("service_down", "auth") in hold
    store.close()


def test_last_ok_sample_allows_service_down_recovery(tmp_path, settings):
    store = _store(tmp_path)
    # 服务恢复(末样本 ok):service_down 不 hold,open 事件可以被 engine 正常判恢复
    _add(store, "auth", [(600, False, None), (300, True, 30.0)])
    findings, hold = evaluate_probe_rules(store, ["auth"], settings, now_ts=NOW, date_str=DATE)
    assert findings == []
    assert ("service_down", "auth") not in hold
    store.close()


def test_latency_exactly_min_samples_evaluates(tmp_path, settings):
    store = _store(tmp_path)
    store.upsert_probe_daily("auth", "2026-06-10", total=288, ok_count=288, p50=80.0, p95=100.0)
    _add(store, "auth", [(i * 300, True, 700.0) for i in range(1, 7)])  # 恰好 6 个 == min_samples
    findings, hold = evaluate_probe_rules(store, ["auth"], settings, now_ts=NOW, date_str=DATE)
    assert [f.rule for f in findings] == ["latency_degraded"]
    assert ("latency_degraded", "auth") not in hold
    store.close()
