# tests/test_crashloop.py
import pytest

from sentinel.scan_docker import detect_crashloops
from sentinel.store import Store


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    from sentinel.config import Settings

    # defaults: window=600, threshold=3
    return Settings(_env_file=None)


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "s.db"))
    yield s
    s.close()


def test_first_observation_seeds_baseline_no_finding(settings, store):
    findings = detect_crashloops(store, settings, {"x": 2}, now_ts=1000)
    assert findings == []
    assert store.get_restart_baseline("x") == (2, 1000)


def test_climb_at_threshold_fires_point_finding_and_resets(settings, store):
    store.upsert_restart_baseline("x", 1, 1000)
    findings = detect_crashloops(store, settings, {"x": 4}, now_ts=1010)
    assert len(findings) == 1
    f = findings[0]
    assert f.rule == "container_crashloop"
    assert f.subject == "x"
    assert f.severity == "warning"
    assert f.point is True
    assert f.needs_diagnosis is True
    assert f.payload["restart_count"] == 4
    assert f.payload["delta"] == 3
    assert f.payload["baseline_count"] == 1
    assert f.payload["window_s"] == 600
    # baseline reset to (rc, now) — next burst must re-accumulate N
    assert store.get_restart_baseline("x") == (4, 1010)


def test_below_threshold_within_window_keeps_accumulating(settings, store):
    store.upsert_restart_baseline("x", 1, 1000)
    findings = detect_crashloops(store, settings, {"x": 2}, now_ts=1010)
    assert findings == []
    # within window (10s < 600s), delta 1 < 3 → baseline untouched
    assert store.get_restart_baseline("x") == (1, 1000)


def test_window_expiry_resets_without_finding(settings, store):
    store.upsert_restart_baseline("x", 5, 1000)
    findings = detect_crashloops(store, settings, {"x": 6}, now_ts=1601)  # 601 >= 600
    assert findings == []
    assert store.get_restart_baseline("x") == (6, 1601)


def test_negative_delta_recreate_resets_without_finding(settings, store):
    store.upsert_restart_baseline("x", 10, 1000)
    findings = detect_crashloops(store, settings, {"x": 0}, now_ts=1010)
    assert findings == []
    assert store.get_restart_baseline("x") == (0, 1010)


def test_multi_container_independent(settings, store):
    store.upsert_restart_baseline("loop", 1, 1000)
    store.upsert_restart_baseline("calm", 1, 1000)
    findings = detect_crashloops(store, settings, {"loop": 5, "calm": 1}, now_ts=1010)
    assert [f.subject for f in findings] == ["loop"]
    assert store.get_restart_baseline("loop") == (5, 1010)  # fired + reset
    assert store.get_restart_baseline("calm") == (1, 1000)  # untouched


def test_baseline_survives_watchmend_restart(settings, tmp_path):
    db = str(tmp_path / "s.db")
    s1 = Store(db)
    detect_crashloops(s1, settings, {"x": 1}, now_ts=1000)  # seeds (1, 1000)
    s1.close()
    # simulate WatchMend restart: fresh Store on same db file
    s2 = Store(db)
    findings = detect_crashloops(s2, settings, {"x": 5}, now_ts=1010)
    assert [f.rule for f in findings] == ["container_crashloop"]
    assert findings[0].payload["delta"] == 4
    s2.close()


def test_stale_baseline_pruned_each_tick(settings, store):
    store.upsert_restart_baseline("gone", 3, 1000)
    # container no longer observed (empty restart_counts); now far past TTL
    findings = detect_crashloops(store, settings, {}, now_ts=1000 + 86401)
    assert findings == []
    assert store.get_restart_baseline("gone") is None  # window_start 1000 < now-86400
