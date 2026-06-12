# src/sentinel/events.py
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sentinel.models import Incident, Indicator


class EventType(StrEnum):
    INCIDENT_OPENED = "incident_opened"
    INCIDENT_UPDATED = "incident_updated"
    INCIDENT_RESOLVED = "incident_resolved"
    COMPONENT_DEGRADED = "component_degraded"
    COMPONENT_RECOVERED = "component_recovered"
    INDICATOR_CHANGED = "indicator_changed"
    BASELINE = "baseline"
    FETCH_FAILED = "fetch_failed"


@dataclass
class TransitionEvent:
    type: EventType
    provider: str
    title: str
    detail: str = ""
    impact: Indicator | None = None
    url: str | None = None
    incident: Incident | None = None
