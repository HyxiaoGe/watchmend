# tests/test_broadcaster.py
import logging

from sentinel.notify.base import Broadcaster
from sentinel.notify.message import Kind, Notification, Severity


class FakeChannel:
    def __init__(self, name, *, fail=False):
        self.name = name
        self.fail = fail
        self.received = []

    async def send(self, n):
        if self.fail:
            raise RuntimeError(f"{self.name} boom")
        self.received.append(n)


def _n():
    return Notification(kind=Kind.ALERT, severity=Severity.WARNING, title="t")


async def test_fan_out_to_all_channels_and_counts_successes():
    a, b = FakeChannel("a"), FakeChannel("b")
    ok = await Broadcaster([a, b]).send(_n())
    assert ok == 2
    assert len(a.received) == 1 and len(b.received) == 1


async def test_one_channel_failure_isolated_others_still_get_it():
    good, bad = FakeChannel("good"), FakeChannel("bad", fail=True)
    ok = await Broadcaster([bad, good]).send(_n())
    assert ok == 1  # 仅 good 成功
    assert len(good.received) == 1  # bad 抛错不影响 good


async def test_all_fail_returns_zero_and_never_raises():
    ok = await Broadcaster([FakeChannel("x", fail=True), FakeChannel("y", fail=True)]).send(_n())
    assert ok == 0  # 调用方据此不 commit


async def test_empty_broadcaster_returns_zero():
    assert await Broadcaster([]).send(_n()) == 0


async def test_failure_logs_channel_name(caplog):
    with caplog.at_level(logging.ERROR, logger="sentinel.notify"):
        await Broadcaster([FakeChannel("telegram", fail=True)]).send(_n())
    assert any("telegram" in r.message for r in caplog.records)
