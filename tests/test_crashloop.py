# tests/test_crashloop.py
import pytest

from sentinel.engine import apply_findings
from sentinel.findings import DOCKER_RULES
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


def test_climb_at_threshold_fires_point_finding(settings, store):
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
    # #2: fire does NOT mutate the baseline — dedup is the engine's cooldown, like the
    # stateless sibling point rules (container_oom / prom container_restart re-derive each tick).
    assert store.get_restart_baseline("x") == (1, 1000)


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
    assert store.get_restart_baseline("loop") == (1, 1000)  # fired but baseline not mutated (#2)
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


# --- #2: fire must not mutate baseline (send-then-commit retry) ---


def test_fire_does_not_reset_baseline(settings, store):
    store.upsert_restart_baseline("x", 1, 1000)
    findings = detect_crashloops(store, settings, {"x": 4}, now_ts=1010)
    assert [f.rule for f in findings] == ["container_crashloop"]
    assert store.get_restart_baseline("x") == (1, 1000)  # baseline left intact


# --- #1: window-freshness guard (no false positive across an observation gap) ---


def test_stale_window_does_not_fire_reseeds(settings, store):
    # window frozen long ago (container absent for a while → detect not called → ws stale);
    # delta>=N but the restarts did NOT happen within W → must NOT fire a false card.
    store.upsert_restart_baseline("x", 5, 1000)
    findings = detect_crashloops(store, settings, {"x": 9}, now_ts=1700)  # now-ws=700 > W=600
    assert findings == []
    assert store.get_restart_baseline("x") == (9, 1700)  # window-expiry reseed instead


def test_window_boundary_exactly_w_still_fires(settings, store):
    # §4.1 preserved: at now-ws == W a genuine in-window burst still fires (guard is <=W).
    store.upsert_restart_baseline("x", 1, 1000)
    findings = detect_crashloops(store, settings, {"x": 4}, now_ts=1600)  # now-ws=600 == W
    assert [f.rule for f in findings] == ["container_crashloop"]


# --- #2 integration: detect_crashloops → apply_findings (cooldown / broadcast retry) ---


class _Broadcaster:
    """Fake broadcaster: returns the queued per-call delivery count (channels reached)."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    async def send(self, _notification):
        self.calls += 1
        return self._results.pop(0) if self._results else 1


async def _tick(store, settings, counts, *, now_ts, broadcaster):
    findings = detect_crashloops(store, settings, counts, now_ts=now_ts)
    await apply_findings(
        findings,
        scope=DOCKER_RULES,
        store=store,
        broadcaster=broadcaster,
        now_ts=now_ts,
        now_str="t",
        cooldown_seconds=21600,
    )


async def test_broadcast_failure_then_retry_delivers_burst(settings, store):
    # a transient all-channel failure at the threshold crossing must NOT silently drop the
    # alert — next tick re-derives the finding (baseline untouched) and delivers it.
    store.upsert_restart_baseline("web", 1, 1000)
    await _tick(store, settings, {"web": 4}, now_ts=1010, broadcaster=_Broadcaster([0]))
    assert store.get_events_since(0) == []  # send-then-commit: nothing committed on failure
    await _tick(store, settings, {"web": 5}, now_ts=1020, broadcaster=_Broadcaster([1]))
    assert [e.rule for e in store.get_events_since(0)] == ["container_crashloop"]  # delivered


async def test_continuous_loop_one_card_per_cooldown(settings, store):
    # cooldown (not the deleted reset-on-fire) dedups: one card per cooldown window.
    store.upsert_restart_baseline("web", 0, 0)
    bc = _Broadcaster([1, 1, 1])
    await _tick(store, settings, {"web": 3}, now_ts=300, broadcaster=bc)  # fire
    await _tick(store, settings, {"web": 6}, now_ts=360, broadcaster=bc)  # in cooldown → suppressed
    assert len(store.get_events_since(0)) == 1
    # continuous observation keeps the window fresh (branch 4 reseeds); after cooldown a fresh
    # in-window burst fires the next card.
    store.upsert_restart_baseline("web", 96, 21600)
    await _tick(store, settings, {"web": 99}, now_ts=21901, broadcaster=bc)  # cooldown over, fresh
    assert len(store.get_events_since(0)) == 2


# --- #3: prune horizon must clamp to the window (no silent miss when W > TTL) ---


@pytest.fixture
def settings_large_window(monkeypatch):
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv("SENTINEL_DOCKER_CRASHLOOP_WINDOW", "172800")  # 48h > 86400 TTL
    from sentinel.config import Settings

    return Settings(_env_file=None)


def test_large_window_slow_loop_not_pruned_mid_window(settings_large_window, store):
    # a routine tick past the 24h TTL but still inside the 48h window (delta<N) must NOT prune
    # the accumulating baseline, else the slow loop is silently never detected.
    detect_crashloops(store, settings_large_window, {"slow": 0}, now_ts=0)  # seed (0,0)
    detect_crashloops(store, settings_large_window, {"slow": 2}, now_ts=90000)  # 25h tick, delta<N
    assert store.get_restart_baseline("slow") == (0, 0)  # survived the prune
    findings = detect_crashloops(store, settings_large_window, {"slow": 3}, now_ts=108000)  # 30h
    assert [f.rule for f in findings] == ["container_crashloop"]
    assert findings[0].payload["delta"] == 3
