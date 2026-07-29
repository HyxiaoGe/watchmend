# tests/test_engine.py
from sentinel.engine import apply_findings
from sentinel.findings import Finding
from sentinel.notify.message import Kind
from sentinel.store import Store

COOLDOWN = 21600


class FakeBroadcaster:
    """记录收到的 Notification;返回成功投递数(fail/fail_first 模拟全渠道失败=0)。"""

    def __init__(self):
        self.sent = []
        self.fail = False
        self.fail_first = False

    async def send(self, n):
        self.sent.append(n)
        if self.fail_first:
            self.fail_first = False
            return 0
        return 0 if self.fail else 1


def _store(tmp_path):
    return Store(str(tmp_path / "e.db"))


def _finding(**kw):
    base = dict(rule="disk_usage", subject="/", severity="critical", detail="使用率 86%")
    base.update(kw)
    return Finding(**base)


async def _apply(findings, store, bc, now_ts, scope=frozenset({"disk_usage"})):
    await apply_findings(
        findings,
        scope=scope,
        store=store,
        broadcaster=bc,
        now_ts=now_ts,
        now_str="n",
        cooldown_seconds=COOLDOWN,
    )


async def test_new_finding_opens_event_and_sends_alert(tmp_path):
    store, bc = _store(tmp_path), FakeBroadcaster()
    await _apply([_finding()], store, bc, 1000)
    assert len(bc.sent) == 1
    assert bc.sent[0].kind is Kind.ALERT
    assert "磁盘水位" in bc.sent[0].title
    opens = store.get_open_events()
    assert len(opens) == 1
    assert opens[0].cooldown_until == 1000 + COOLDOWN
    assert opens[0].diagnosis_status == "skipped"  # needs_diagnosis=False
    store.close()


async def test_open_event_not_resent(tmp_path):
    store, bc = _store(tmp_path), FakeBroadcaster()
    await _apply([_finding()], store, bc, 1000)
    await _apply([_finding()], store, bc, 1900)
    assert len(bc.sent) == 1
    assert len(store.get_open_events()) == 1
    store.close()


async def test_recovery_resolves_and_sends_recovery(tmp_path):
    store, bc = _store(tmp_path), FakeBroadcaster()
    await _apply([_finding()], store, bc, 1000)
    await _apply([], store, bc, 2000)
    assert len(bc.sent) == 2
    assert bc.sent[1].kind is Kind.RECOVERY
    assert store.get_open_events() == []
    assert store.count_resolved_since(0) == 1
    store.close()


async def test_out_of_scope_open_event_untouched(tmp_path):
    store, bc = _store(tmp_path), FakeBroadcaster()
    await _apply([_finding()], store, bc, 1000)
    await _apply([], store, bc, 2000, scope=frozenset({"mem_pressure"}))
    assert len(bc.sent) == 1  # 没发恢复
    assert len(store.get_open_events()) == 1
    store.close()


async def test_cooldown_blocks_refire_after_resolve(tmp_path):
    store, bc = _store(tmp_path), FakeBroadcaster()
    await _apply([_finding()], store, bc, 1000)
    await _apply([], store, bc, 2000)
    await _apply([_finding()], store, bc, 3000)
    assert len(bc.sent) == 2
    assert store.get_open_events() == []
    await _apply([_finding()], store, bc, 1000 + COOLDOWN + 1)
    assert len(bc.sent) == 3
    assert len(store.get_open_events()) == 1
    store.close()


async def test_point_event_resolved_immediately_no_recovery(tmp_path):
    store, bc = _store(tmp_path), FakeBroadcaster()
    f = _finding(
        rule="container_restart",
        subject="dozzle",
        severity="warning",
        point=True,
        needs_diagnosis=True,
    )
    scope = frozenset({"container_restart"})
    await _apply([f], store, bc, 1000, scope=scope)
    assert len(bc.sent) == 1
    assert store.get_open_events() == []
    assert store.get_pending_diagnosis_events()[0].rule == "container_restart"
    await _apply([], store, bc, 1900, scope=scope)
    assert len(bc.sent) == 1
    await _apply([f], store, bc, 2000, scope=scope)
    assert len(bc.sent) == 1
    store.close()


async def test_send_failure_not_committed_then_retried(tmp_path):
    store, bc = _store(tmp_path), FakeBroadcaster()
    bc.fail = True
    await _apply([_finding()], store, bc, 1000)  # 全渠道失败 → 返回 0,不提交
    assert store.get_open_events() == []
    bc.fail = False
    await _apply([_finding()], store, bc, 2000)
    assert len([n for n in bc.sent if n.kind is Kind.ALERT]) == 2  # 第一次尝试也记到 sent
    assert len(store.get_open_events()) == 1
    store.close()


async def test_duplicate_findings_same_round_open_once(tmp_path):
    store, bc = _store(tmp_path), FakeBroadcaster()
    await apply_findings(
        [_finding(), _finding()],
        scope=frozenset({"disk_usage"}),
        store=store,
        broadcaster=bc,
        now_ts=1000,
        now_str="n",
        cooldown_seconds=0,
    )
    assert len(bc.sent) == 1
    assert len(store.get_open_events()) == 1
    store.close()


async def test_one_send_failure_does_not_block_others(tmp_path):
    store, bc = _store(tmp_path), FakeBroadcaster()
    bc.fail_first = True
    a = _finding(subject="/a")
    b = _finding(subject="/b")
    await _apply([a, b], store, bc, 1000)
    assert [e.subject for e in store.get_open_events()] == ["/b"]  # /a 第一次失败未提交
    store.close()


async def test_recovery_send_failure_keeps_open_then_retried(tmp_path):
    store, bc = _store(tmp_path), FakeBroadcaster()
    await _apply([_finding()], store, bc, 1000)
    bc.fail = True
    await _apply([], store, bc, 2000)  # 恢复全渠道失败 → 不 resolve
    assert len(store.get_open_events()) == 1
    bc.fail = False
    await _apply([], store, bc, 3000)
    assert store.get_open_events() == []
    store.close()


async def test_hold_blocks_recovery_without_firing(tmp_path):
    store, bc = _store(tmp_path), FakeBroadcaster()
    lat = _finding(rule="latency_degraded", subject="auth", severity="warning", detail="慢")
    scope = frozenset({"latency_degraded"})
    await _apply([lat], store, bc, 1000, scope=scope)
    await apply_findings(
        [],
        scope=scope,
        store=store,
        broadcaster=bc,
        now_ts=2000,
        now_str="n",
        cooldown_seconds=COOLDOWN,
        hold={("latency_degraded", "auth")},
    )
    assert len(bc.sent) == 1  # 没发恢复
    assert len(store.get_open_events()) == 1
    store.close()


async def test_nonurgent_finding_is_deferred_into_digest(tmp_path):
    store, bc = _store(tmp_path), FakeBroadcaster()
    finding = _finding(
        rule="latency_degraded",
        subject="auth",
        severity="warning",
        detail="P95 延迟升高",
        needs_diagnosis=True,
    )
    scope = frozenset({"latency_degraded"})
    await apply_findings(
        [finding],
        scope=scope,
        store=store,
        broadcaster=bc,
        now_ts=1000,
        now_str="n",
        cooldown_seconds=COOLDOWN,
        defer_nonurgent=True,
    )
    assert bc.sent == []
    event = store.get_open_events()[0]
    assert event.notified is False
    assert event.diagnosis_status == "skipped"
    assert [(item.rule, item.subject, item.state) for item in store.get_pending_digest_items()] == [
        ("latency_degraded", "auth", "observed")
    ]

    await apply_findings(
        [],
        scope=scope,
        store=store,
        broadcaster=bc,
        now_ts=2000,
        now_str="n",
        cooldown_seconds=COOLDOWN,
        defer_nonurgent=True,
    )
    assert bc.sent == []
    assert store.get_open_events() == []
    assert store.get_pending_digest_items()[0].state == "resolved"
    store.close()


async def test_hard_alert_stays_immediate_when_defer_enabled(tmp_path):
    store, bc = _store(tmp_path), FakeBroadcaster()
    finding = _finding(
        rule="service_down",
        subject="auth",
        severity="critical",
        detail="连续 3 次失败",
        needs_diagnosis=True,
    )
    await apply_findings(
        [finding],
        scope=frozenset({"service_down"}),
        store=store,
        broadcaster=bc,
        now_ts=1000,
        now_str="n",
        cooldown_seconds=COOLDOWN,
        defer_nonurgent=True,
    )
    assert len(bc.sent) == 1
    event = store.get_open_events()[0]
    assert event.notified is True
    assert event.diagnosis_status == "pending"
    assert store.get_pending_digest_items() == []
    store.close()


async def test_deferred_new_events_respect_per_cycle_cap(tmp_path):
    store, bc = _store(tmp_path), FakeBroadcaster()
    findings = [
        _finding(
            rule="log_error_new",
            subject=f"worker · error-{index}",
            severity="warning",
            point=True,
        )
        for index in range(3)
    ]
    await apply_findings(
        findings,
        scope=frozenset({"log_error_new"}),
        store=store,
        broadcaster=bc,
        now_ts=1000,
        now_str="n",
        cooldown_seconds=COOLDOWN,
        defer_nonurgent=True,
        max_new_sends=2,
    )
    assert bc.sent == []
    assert len(store.get_pending_digest_items()) == 2
    assert store.count_resolved_since(0) == 2
    store.close()
