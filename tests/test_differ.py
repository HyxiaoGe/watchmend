# tests/test_differ.py
from sentinel.differ import diff
from sentinel.events import EventType
from sentinel.models import (
    Component,
    ComponentStatus,
    Incident,
    IncidentStatus,
    Indicator,
    Snapshot,
)


def _snap(indicator=Indicator.NONE, components=None, incidents=None):
    return Snapshot(
        provider="anthropic",
        display_name="Anthropic",
        indicator=indicator,
        status_url="https://status.claude.com",
        components=components or [],
        incidents=incidents or [],
        fetched_at="2026-06-06T00:00:00Z",
    )


def _inc(key, status=IncidentStatus.INVESTIGATING, update_id="u1"):
    return Incident(
        key=key,
        title=f"incident {key}",
        status=status,
        impact=Indicator.MAJOR,
        url="https://x",
        started_at=None,
        updated_at=None,
        latest_update_id=update_id,
    )


def _types(events):
    return [e.type for e in events]


def test_cold_start_all_green_no_events():
    assert diff(None, _snap()) == []


def test_cold_start_with_incident_emits_baseline():
    events = diff(None, _snap(Indicator.MAJOR, incidents=[_inc("i1")]))
    assert _types(events) == [EventType.BASELINE]


def test_new_incident_opened():
    old = _snap()
    new = _snap(Indicator.MAJOR, incidents=[_inc("i1")])
    assert EventType.INCIDENT_OPENED in _types(diff(old, new))


def test_incident_disappears_is_resolved():
    old = _snap(Indicator.MAJOR, incidents=[_inc("i1")])
    new = _snap()  # i1 没了
    assert EventType.INCIDENT_RESOLVED in _types(diff(old, new))


def test_phase_change_emits_updated():
    old = _snap(Indicator.MAJOR, incidents=[_inc("i1", IncidentStatus.INVESTIGATING)])
    new = _snap(Indicator.MAJOR, incidents=[_inc("i1", IncidentStatus.MONITORING)])
    assert EventType.INCIDENT_UPDATED in _types(diff(old, new))


def test_text_only_update_suppressed_under_phase_but_emitted_under_all():
    old = _snap(Indicator.MAJOR, incidents=[_inc("i1", IncidentStatus.MONITORING, "u1")])
    new = _snap(Indicator.MAJOR, incidents=[_inc("i1", IncidentStatus.MONITORING, "u2")])
    assert EventType.INCIDENT_UPDATED not in _types(diff(old, new, verbosity="phase"))
    assert EventType.INCIDENT_UPDATED in _types(diff(old, new, verbosity="all"))


def test_component_degraded_and_recovered():
    op = Component(key="c", name="API", status=ComponentStatus.OPERATIONAL)
    bad = Component(key="c", name="API", status=ComponentStatus.MAJOR_OUTAGE)
    assert EventType.COMPONENT_DEGRADED in _types(
        diff(_snap(components=[op]), _snap(components=[bad]))
    )
    assert EventType.COMPONENT_RECOVERED in _types(
        diff(_snap(components=[bad]), _snap(components=[op]))
    )


def test_none_minor_indicator_flap_without_incident_is_suppressed():
    old = _snap(Indicator.NONE)
    new = _snap(Indicator.MINOR)  # 纯指标抖动,无 incident
    assert EventType.INDICATOR_CHANGED not in _types(diff(old, new))


def test_escalation_to_major_indicator_always_alerts():
    old = _snap(Indicator.MINOR)
    new = _snap(Indicator.MAJOR)
    assert EventType.INDICATOR_CHANGED in _types(diff(old, new))


def test_cold_start_bare_minor_without_incident_no_baseline():
    # 裸 minor(无 incident、无非运营组件)= 装饰性噪音(如 Cloudflare 常态 POP 维护),
    # 启动基线须与稳态同样抑制,不发空卡。
    assert diff(None, _snap(Indicator.MINOR)) == []


def test_cold_start_minor_with_degraded_component_still_baselines():
    # minor 但有被跟踪的非运营组件 = 有实质内容,启动基线仍应发(且可指明组件)。
    bad = Component(key="c", name="API", status=ComponentStatus.PARTIAL_OUTAGE)
    events = diff(None, _snap(Indicator.MINOR, components=[bad]))
    assert _types(events) == [EventType.BASELINE]


def test_cold_start_major_indicator_without_incident_still_baselines():
    # 重大指标(major+)即使没有 incident 也是真实信号,启动基线必发。
    assert _types(diff(None, _snap(Indicator.MAJOR))) == [EventType.BASELINE]
