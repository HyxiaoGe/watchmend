# tests/test_models.py
from sentinel.models import (
    Component,
    ComponentStatus,
    Incident,
    IncidentStatus,
    Indicator,
    Snapshot,
    snapshot_from_dict,
    snapshot_to_dict,
)


def test_indicator_rank_orders_severity():
    assert (
        Indicator.CRITICAL.rank > Indicator.MAJOR.rank > Indicator.MINOR.rank > Indicator.NONE.rank
    )


def test_component_status_rank_orders_severity():
    assert ComponentStatus.MAJOR_OUTAGE.rank > ComponentStatus.PARTIAL_OUTAGE.rank
    assert ComponentStatus.OPERATIONAL.rank == 0


def test_snapshot_roundtrips_through_dict():
    snap = Snapshot(
        provider="anthropic",
        display_name="Anthropic / Claude",
        indicator=Indicator.MAJOR,
        status_url="https://status.claude.com",
        components=[Component(key="c1", name="API", status=ComponentStatus.PARTIAL_OUTAGE)],
        incidents=[
            Incident(
                key="i1",
                title="API errors",
                status=IncidentStatus.MONITORING,
                impact=Indicator.MAJOR,
                url="https://stspg.io/x",
                started_at="2026-06-06T00:00:00Z",
                updated_at="2026-06-06T01:00:00Z",
                latest_update_id="u9",
                affected=["API"],
            )
        ],
        fetched_at="2026-06-06T01:00:00Z",
    )
    restored = snapshot_from_dict(snapshot_to_dict(snap))
    assert restored == snap
    # enums survive as enums, not raw strings
    assert restored.indicator is Indicator.MAJOR
    assert restored.incidents[0].status is IncidentStatus.MONITORING
