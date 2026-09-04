from __future__ import annotations

from sentinel.codex_reset.models import ResetEvidence, ResetStage, ResetType
from sentinel.codex_reset.sources import canonical_from


def confirmation_basis(evidence: list[ResetEvidence]) -> str:
    """转载不增加独立证据；本机窗口必须对应公开重置事实或正式预告。"""
    confirmations = [
        item
        for item in evidence
        if item.signal_stage is ResetStage.CONFIRMED and item.explicit_completed
    ]
    public = [item for item in confirmations if not item.local_reference and item.url]
    if any(item.local_reference and item.reset_type is ResetType.BANKED for item in confirmations):
        return "本机参考账号 Banked reset 可用数量增加"
    announced = any(
        item.signal_stage is ResetStage.ANNOUNCED
        and item.official
        and item.reset_type is ResetType.DIRECT
        and item.expected_end_ts is not None
        for item in evidence
    )
    official_completed = any(
        item.official and item.reset_type is ResetType.DIRECT for item in public
    )
    authoritative_live_post = any(
        item.official
        and item.reset_type is ResetType.DIRECT
        and item.source_name in {"reset_feed", "reset_timeline"}
        and item.title == "Live radar feed"
        for item in public
    )
    if authoritative_live_post:
        return "官方明确到账原帖"
    if any(item.local_reference for item in confirmations) and (announced or official_completed):
        return "官方重置记录 + 本机共享 Codex 周额度窗口"
    for first in public:
        for second in public:
            if first.source_family != second.source_family and canonical_from(
                first.url, first.source_item_id
            ) != canonical_from(second.url, second.source_item_id):
                return "不同来源族的独立公开重置记录"
    return ""
