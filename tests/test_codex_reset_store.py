from sentinel.codex_reset.models import ResetEvidence, ResetStage, ResetType
from sentinel.codex_reset.store import ResetStore


def _evidence(stage: ResetStage, *, family="a", item="1", observed=1000):
    return ResetEvidence(
        source_name=f"source-{family}",
        source_family=family,
        source_item_id=item,
        canonical_hint="x:1",
        signal_stage=stage,
        title="reset",
        summary="official reset signal",
        url="https://x.com/thsottiaux/status/1",
        observed_at=observed,
        reset_type=ResetType.DIRECT,
        expected_start_ts=1100 if stage is ResetStage.ANNOUNCED else None,
        expected_end_ts=1200 if stage is ResetStage.ANNOUNCED else None,
        official=True,
        explicit_completed=stage is ResetStage.CONFIRMED,
    )


def test_stage_upgrade_hint_announced_delayed_confirmed(tmp_path):
    store = ResetStore(str(tmp_path / "s.db"))
    hint = _evidence(ResetStage.HINT)
    store.put_evidence("x:1", hint, now_ts=1000)
    event, advanced = store.upsert_event("x:1", ResetStage.HINT, [hint], now_ts=1000)
    assert advanced and event.stage is ResetStage.HINT

    announced = _evidence(ResetStage.ANNOUNCED, item="2", observed=1050)
    store.put_evidence("x:1", announced, now_ts=1050)
    event, advanced = store.upsert_event(
        "x:1", ResetStage.ANNOUNCED, store.evidence_for("x:1"), now_ts=1050
    )
    assert advanced and event.stage is ResetStage.ANNOUNCED
    delayed = store.mark_delayed(now_ts=1301, grace_seconds=100)
    assert delayed[0].stage is ResetStage.DELAYED

    confirmed = _evidence(ResetStage.CONFIRMED, family="b", item="3", observed=1350)
    store.put_evidence("x:1", confirmed, now_ts=1350)
    event, advanced = store.upsert_event(
        "x:1", ResetStage.CONFIRMED, store.evidence_for("x:1"), now_ts=1350
    )
    assert advanced and event.stage is ResetStage.CONFIRMED
    store.close()


def test_each_stage_has_independent_deduplicated_delivery_receipt(tmp_path):
    store = ResetStore(str(tmp_path / "s.db"))
    hint = _evidence(ResetStage.HINT)
    store.put_evidence("x:1", hint, now_ts=1000)
    store.upsert_event("x:1", ResetStage.HINT, [hint], now_ts=1000)
    store.queue_delivery("x:1", ResetStage.HINT, now_ts=1000)
    store.queue_delivery("x:1", ResetStage.HINT, now_ts=1000)
    store.queue_delivery("x:1", ResetStage.ANNOUNCED, now_ts=1000)
    assert len(store.due_deliveries(now_ts=1000)) == 2
    store.mark_delivery_success("x:1", ResetStage.HINT, delivered_ts=1001)
    assert store.delivery_status("x:1", ResetStage.HINT) == ("delivered", 0)
    assert store.delivery_status("x:1", ResetStage.ANNOUNCED) == ("pending", 0)
    store.close()


def test_sqlite_lease_excludes_second_instance_and_expires(tmp_path):
    path = str(tmp_path / "s.db")
    first, second = ResetStore(path), ResetStore(path)
    assert first.acquire_lease("reset", "one", now_ts=100, ttl_seconds=60)
    assert not second.acquire_lease("reset", "two", now_ts=120, ttl_seconds=60)
    assert second.acquire_lease("reset", "two", now_ts=161, ttl_seconds=60)
    first.close()
    second.close()


def test_source_health_tolerates_one_source_failure(tmp_path):
    store = ResetStore(str(tmp_path / "s.db"))
    store.record_source_success("a", "family-a", now_ts=1000, content_ts=990, item_count=1)
    store.record_source_success("b", "family-b", now_ts=1000, content_ts=995, item_count=1)
    store.record_source_failure("c", "family-c", now_ts=1000, error="TimeoutError")
    assert store.health(now_ts=1010, freshness_seconds=60)["status"] == "ok"
    store.record_source_failure("b", "family-b", now_ts=1020, error="TimeoutError")
    # 最近一次成功内容仍新鲜；瞬时失败不让整体失效。
    assert store.health(now_ts=1030, freshness_seconds=60)["status"] == "ok"
    assert store.health(now_ts=1100, freshness_seconds=60)["status"] == "stale"
    store.close()


def test_confirmation_parent_and_reply_merge_by_time(tmp_path):
    store = ResetStore(str(tmp_path / "s.db"))
    first = _evidence(ResetStage.CONFIRMED, family="a", item="parent", observed=1000)
    store.put_evidence("x:parent", first, now_ts=1000)
    assert store.find_confirmation_evidence_target(1050) == "x:parent"
    store.close()
