# src/sentinel/differ.py
from __future__ import annotations

from sentinel.events import EventType, TransitionEvent
from sentinel.models import Component, ComponentStatus, Incident, Indicator, Snapshot

_COSMETIC = {Indicator.NONE, Indicator.MINOR}


def diff(old: Snapshot | None, new: Snapshot, *, verbosity: str = "phase") -> list[TransitionEvent]:
    """比对上次/本次快照得翻转事件。old=None 为冷启动(只发非绿基线)。

    依赖适配器契约:Snapshot.incidents[] 只含当前未解决 → 「消失=已解决」对所有 provider 统一成立。
    """
    if old is None:
        return _baseline(new)

    events: list[TransitionEvent] = []
    old_inc = {i.key: i for i in old.incidents}
    new_inc = {i.key: i for i in new.incidents}

    for key, inc in new_inc.items():
        prev = old_inc.get(key)
        if prev is None:
            events.append(_incident_event(EventType.INCIDENT_OPENED, new.provider, inc, "新事件"))
        else:
            phase_changed = prev.status != inc.status
            update_changed = prev.latest_update_id != inc.latest_update_id
            if phase_changed or (verbosity == "all" and update_changed):
                events.append(
                    _incident_event(EventType.INCIDENT_UPDATED, new.provider, inc, "事件更新")
                )

    for key, inc in old_inc.items():
        if key not in new_inc:
            events.append(_incident_event(EventType.INCIDENT_RESOLVED, new.provider, inc, "已恢复"))

    events.extend(_component_events(old, new))

    incident_changed = bool(events)
    if old.indicator != new.indicator:
        cosmetic = {old.indicator, new.indicator} <= _COSMETIC
        if not (cosmetic and not incident_changed):
            title = f"{new.display_name} 总指标 {old.indicator.value} → {new.indicator.value}"
            events.append(
                TransitionEvent(
                    type=EventType.INDICATOR_CHANGED,
                    provider=new.provider,
                    title=title,
                    impact=new.indicator,
                    url=new.status_url,
                )
            )
    return events


def _baseline(new: Snapshot) -> list[TransitionEvent]:
    # 与稳态同款装饰性抑制:裸 minor/none(无 incident、无非运营组件)不发基线。
    # 否则像 Cloudflare 常年 minor(数十个 POP 轮换维护、0 真实事件)会发零信息空卡。
    substantive = (
        bool(new.incidents)
        or any(c.status is not ComponentStatus.OPERATIONAL for c in new.components)
        or new.indicator not in (Indicator.NONE, Indicator.MINOR, Indicator.UNKNOWN)
    )
    if not substantive:
        return []
    detail = f"指标={new.indicator.value};未解决事件={len(new.incidents)}"
    return [
        TransitionEvent(
            type=EventType.BASELINE,
            provider=new.provider,
            title=f"{new.display_name} 启动基线(非新事件)",
            detail=detail,
            url=new.status_url,
        )
    ]


def _component_events(old: Snapshot, new: Snapshot) -> list[TransitionEvent]:
    events: list[TransitionEvent] = []
    old_comp = {c.key: c for c in old.components}
    for _key, comp in {c.key: c for c in new.components}.items():
        prev = old_comp.get(comp.key)
        if prev is None or prev.status == comp.status:
            continue
        if comp.status is ComponentStatus.OPERATIONAL:
            events.append(
                _component_event(EventType.COMPONENT_RECOVERED, new.provider, comp, "恢复")
            )
        elif comp.status.rank > prev.status.rank:
            events.append(
                _component_event(EventType.COMPONENT_DEGRADED, new.provider, comp, "降级")
            )
    return events


def _incident_event(etype: EventType, provider: str, inc: Incident, verb: str) -> TransitionEvent:
    return TransitionEvent(
        type=etype,
        provider=provider,
        title=f"{verb}:{inc.title}",
        detail=f"状态={inc.status.value};影响={inc.impact.value}"
        + (f";产品={', '.join(inc.affected)}" if inc.affected else ""),
        impact=inc.impact,
        url=inc.url,
        incident=inc,
    )


def _component_event(
    etype: EventType, provider: str, comp: Component, verb: str
) -> TransitionEvent:
    return TransitionEvent(
        type=etype,
        provider=provider,
        title=f"组件{verb}:{comp.name}",
        detail=f"状态={comp.status.value}",
    )
