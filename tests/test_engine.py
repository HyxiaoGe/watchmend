# tests/test_engine.py
from sentinel.engine import apply_findings
from sentinel.findings import Finding
from sentinel.store import Store

COOLDOWN = 21600


class FakeFeishu:
    def __init__(self):
        self.sent = []
        self.fail = False
        self.fail_first = False

    async def send(self, card):
        if self.fail_first:
            self.fail_first = False
            raise RuntimeError("send failed")
        if self.fail:
            raise RuntimeError("send failed")
        self.sent.append(card)


def _store(tmp_path):
    return Store(str(tmp_path / "e.db"))


def _finding(**kw):
    base = dict(rule="disk_usage", subject="/", severity="critical", detail="使用率 86%")
    base.update(kw)
    return Finding(**base)


async def _apply(findings, store, feishu, now_ts, scope=frozenset({"disk_usage"})):
    await apply_findings(
        findings,
        scope=scope,
        store=store,
        feishu=feishu,
        now_ts=now_ts,
        now_str="n",
        cooldown_seconds=COOLDOWN,
    )


async def test_new_finding_opens_event_and_sends_card(tmp_path):
    store, feishu = _store(tmp_path), FakeFeishu()
    await _apply([_finding()], store, feishu, 1000)
    assert len(feishu.sent) == 1
    assert "磁盘水位" in feishu.sent[0]["card"]["header"]["title"]["content"]
    opens = store.get_open_events()
    assert len(opens) == 1
    assert opens[0].cooldown_until == 1000 + COOLDOWN
    assert opens[0].diagnosis_status == "skipped"  # needs_diagnosis=False
    store.close()


async def test_open_event_not_resent(tmp_path):
    store, feishu = _store(tmp_path), FakeFeishu()
    await _apply([_finding()], store, feishu, 1000)
    await _apply([_finding()], store, feishu, 1900)
    assert len(feishu.sent) == 1
    assert len(store.get_open_events()) == 1
    store.close()


async def test_recovery_resolves_and_sends_green(tmp_path):
    store, feishu = _store(tmp_path), FakeFeishu()
    await _apply([_finding()], store, feishu, 1000)
    await _apply([], store, feishu, 2000)
    assert len(feishu.sent) == 2
    assert feishu.sent[1]["card"]["header"]["template"] == "green"
    assert store.get_open_events() == []
    assert store.count_resolved_since(0) == 1
    store.close()


async def test_out_of_scope_open_event_untouched(tmp_path):
    store, feishu = _store(tmp_path), FakeFeishu()
    await _apply([_finding()], store, feishu, 1000)
    await _apply([], store, feishu, 2000, scope=frozenset({"mem_pressure"}))
    assert len(feishu.sent) == 1  # 没发恢复卡
    assert len(store.get_open_events()) == 1  # 还 open
    store.close()


async def test_cooldown_blocks_refire_after_resolve(tmp_path):
    store, feishu = _store(tmp_path), FakeFeishu()
    await _apply([_finding()], store, feishu, 1000)
    await _apply([], store, feishu, 2000)  # 恢复
    await _apply([_finding()], store, feishu, 3000)  # 冷却内再命中
    assert len(feishu.sent) == 2  # 没有第三张卡
    assert store.get_open_events() == []
    await _apply([_finding()], store, feishu, 1000 + COOLDOWN + 1)  # 冷却过了
    assert len(feishu.sent) == 3
    assert len(store.get_open_events()) == 1
    store.close()


async def test_point_event_resolved_immediately_no_recovery(tmp_path):
    store, feishu = _store(tmp_path), FakeFeishu()
    f = _finding(
        rule="container_restart",
        subject="dozzle",
        severity="warning",
        point=True,
        needs_diagnosis=True,
    )
    scope = frozenset({"container_restart"})
    await _apply([f], store, feishu, 1000, scope=scope)
    assert len(feishu.sent) == 1
    assert store.get_open_events() == []  # 落库即 resolved
    assert store.get_pending_diagnosis_events()[0].rule == "container_restart"
    await _apply([], store, feishu, 1900, scope=scope)
    assert len(feishu.sent) == 1  # 无恢复卡
    await _apply([f], store, feishu, 2000, scope=scope)
    assert len(feishu.sent) == 1  # 冷却去重([1h] 窗口同一次重启会被连续扫到)
    store.close()


async def test_send_failure_not_committed_then_retried(tmp_path):
    store, feishu = _store(tmp_path), FakeFeishu()
    feishu.fail = True
    await _apply([_finding()], store, feishu, 1000)  # 不抛出
    assert store.get_open_events() == []  # send-then-commit:没发出去就不落库
    feishu.fail = False
    await _apply([_finding()], store, feishu, 2000)
    assert len(feishu.sent) == 1
    assert len(store.get_open_events()) == 1
    store.close()


async def test_duplicate_findings_same_round_open_once(tmp_path):
    store, feishu = _store(tmp_path), FakeFeishu()
    await apply_findings(
        [_finding(), _finding()],
        scope=frozenset({"disk_usage"}),
        store=store,
        feishu=feishu,
        now_ts=1000,
        now_str="n",
        cooldown_seconds=0,  # 冷却 0 也不双开
    )
    assert len(feishu.sent) == 1
    assert len(store.get_open_events()) == 1
    store.close()


async def test_one_send_failure_does_not_block_others(tmp_path):
    store, feishu = _store(tmp_path), FakeFeishu()
    feishu.fail_first = True
    a = _finding(subject="/a")
    b = _finding(subject="/b")
    await _apply([a, b], store, feishu, 1000)
    assert len(feishu.sent) == 1  # 第一张失败,第二张照发
    assert [e.subject for e in store.get_open_events()] == ["/b"]
    store.close()


async def test_recovery_send_failure_keeps_open_then_retried(tmp_path):
    store, feishu = _store(tmp_path), FakeFeishu()
    await _apply([_finding()], store, feishu, 1000)
    feishu.fail = True
    await _apply([], store, feishu, 2000)  # 恢复卡发送失败:不 resolve
    assert len(store.get_open_events()) == 1
    feishu.fail = False
    await _apply([], store, feishu, 3000)
    assert len(feishu.sent) == 2
    assert store.get_open_events() == []
    store.close()


async def test_hold_blocks_recovery_without_firing(tmp_path):
    store, feishu = _store(tmp_path), FakeFeishu()
    lat = _finding(rule="latency_degraded", subject="auth", severity="warning", detail="慢")
    scope = frozenset({"latency_degraded"})
    await _apply([lat], store, feishu, 1000, scope=scope)
    # 服务随后挂掉:latency 本轮被跳过评估(不在 findings),hold 保护不被误判恢复
    await apply_findings(
        [],
        scope=scope,
        store=store,
        feishu=feishu,
        now_ts=2000,
        now_str="n",
        cooldown_seconds=COOLDOWN,
        hold={("latency_degraded", "auth")},
    )
    assert len(feishu.sent) == 1  # 没发恢复卡
    assert len(store.get_open_events()) == 1  # 还 open
    store.close()
