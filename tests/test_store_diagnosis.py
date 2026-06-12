import json

import pytest

from sentinel.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    yield s
    s.close()


def _insert(store, *, rule="mem_pressure", subject="swap", diagnosis_status="pending"):
    return store.insert_event(
        ts=1000,
        rule=rule,
        subject=subject,
        severity="warning",
        status="open",
        detail="d",
        payload_json="{}",
        diagnosis_status=diagnosis_status,
        cooldown_until=0,
    )


def test_set_diagnosis_done_removes_from_pending(store):
    eid = _insert(store)
    assert [e.id for e in store.get_pending_diagnosis_events()] == [eid]
    ok = store.set_diagnosis(eid, status="done", diagnosis_json=json.dumps({"summary": "x"}))
    assert ok is True
    assert store.get_pending_diagnosis_events() == []
    ev = store.get_event(eid)
    assert ev.diagnosis_status == "done"
    assert json.loads(ev.diagnosis_json) == {"summary": "x"}


def test_set_diagnosis_failed_without_json(store):
    eid = _insert(store)
    assert store.set_diagnosis(eid, status="failed") is True
    ev = store.get_event(eid)
    assert ev.diagnosis_status == "failed"
    assert ev.diagnosis_json is None


def test_set_diagnosis_rejects_invalid_status(store):
    eid = _insert(store)
    with pytest.raises(ValueError):
        store.set_diagnosis(eid, status="pending")  # 不允许写回 pending


def test_set_diagnosis_missing_event_returns_false(store):
    assert store.set_diagnosis(999, status="done") is False


def test_get_event_missing_returns_none(store):
    assert store.get_event(42) is None
