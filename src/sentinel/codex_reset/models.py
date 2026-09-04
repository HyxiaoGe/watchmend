from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ResetStage(StrEnum):
    HINT = "hint"
    ANNOUNCED = "announced"
    DELAYED = "delayed"
    CONFIRMED = "confirmed"

    @property
    def rank(self) -> int:
        return {
            ResetStage.HINT: 1,
            ResetStage.ANNOUNCED: 2,
            ResetStage.DELAYED: 3,
            ResetStage.CONFIRMED: 4,
        }[self]


class ResetType(StrEnum):
    DIRECT = "direct"
    BANKED = "banked"


@dataclass(frozen=True)
class BankedResetBalance:
    source_name: str
    available_count: int
    observed_at: int


@dataclass(frozen=True)
class ResetIntentCandidate:
    source_name: str
    source_family: str
    source_item_id: str
    text: str
    url: str
    observed_at: int


@dataclass(frozen=True)
class ResetEvidence:
    source_name: str
    source_family: str
    source_item_id: str
    canonical_hint: str
    signal_stage: ResetStage
    title: str
    summary: str
    url: str
    observed_at: int
    reset_type: ResetType | None = None
    expected_start_ts: int | None = None
    expected_end_ts: int | None = None
    official: bool = False
    explicit_completed: bool = False
    local_reference: bool = False


@dataclass(frozen=True)
class FetchedSource:
    name: str
    family: str
    content_ts: int | None
    evidence: list[ResetEvidence] = field(default_factory=list)
    intent_candidates: list[ResetIntentCandidate] = field(default_factory=list)
    banked_balances: list[BankedResetBalance] = field(default_factory=list)


@dataclass(frozen=True)
class ResetEvent:
    canonical_id: str
    stage: ResetStage
    reset_type: ResetType | None
    title: str
    summary: str
    primary_url: str
    announced_ts: int | None
    expected_start_ts: int | None
    expected_end_ts: int | None
    confirmed_ts: int | None
    first_seen_ts: int
    last_seen_ts: int
    evidence_count: int = 0
    source_families: tuple[str, ...] = ()
    translated_summary: str = ""
    had_preannouncement: bool = False
    confirmation_basis: str = ""
    evidence_start_ts: int | None = None
    evidence_end_ts: int | None = None

    @property
    def silent(self) -> bool:
        return (
            self.stage is ResetStage.CONFIRMED
            and self.reset_type is ResetType.DIRECT
            and self.announced_ts is None
            and not self.had_preannouncement
        )


def can_advance(current: ResetStage | None, target: ResetStage) -> bool:
    """阶段只前进；confirmed 是终态，delayed 可继续到 confirmed。"""
    return current is None or target.rank > current.rank
