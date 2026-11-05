"""日报当前状态：与近 24 小时历史统计分开判断。"""

from dataclasses import dataclass

from sentinel.findings import EventRecord
from sentinel.models import ServiceDayStats


@dataclass(frozen=True)
class ReportState:
    level: str
    label: str


def assess_current_report(
    stats: list[ServiceDayStats], open_events: list[EventRecord]
) -> ReportState:
    """仅未决事件和采样缺失影响当前状态，历史失败保留作回顾。"""
    missing = sum(s.total == 0 for s in stats)
    labels = []
    if open_events:
        labels.append(f"当前有 {len(open_events)} 起未决事件")
    if missing:
        labels.append(f"采样缺失 {missing} 项，状态待确认")
    if any(e.severity == "critical" for e in open_events) or missing:
        return ReportState("critical", " · ".join(labels))
    if open_events:
        return ReportState("warning", " · ".join(labels))
    if not stats:
        return ReportState("unknown", "暂无监控数据")
    return ReportState("info", "当前无未决异常")
