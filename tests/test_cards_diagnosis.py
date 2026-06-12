# tests/test_cards_diagnosis.py
import pytest

from sentinel.feishu.cards import build_diagnosis_card, build_summary_card
from sentinel.findings import EventRecord


def _event(**kw):
    base = dict(
        id=7,
        ts=1000,
        rule="mem_pressure",
        subject="swap",
        severity="warning",
        status="open",
        detail="swap 83.4% > 80%",
        payload_json="{}",
        diagnosis_status="pending",
        diagnosis_json=None,
        cooldown_until=0,
        resolved_ts=None,
    )
    base.update(kw)
    return EventRecord(**base)


def test_diagnosis_card_structure():
    diagnosis = {
        "summary": "swap 持续高位",
        "root_cause": "postgres 容器冷数据被换出",
        "evidence": ["loki 无 OOM 日志", "node_memory_SwapFree 持续下降"],
        "suggested_commands": ["docker stats --no-stream"],
        "confidence": "medium",
    }
    card = build_diagnosis_card(_event(), diagnosis, now_str="2026-06-12 10:00:00")
    assert card["msg_type"] == "interactive"
    assert card["card"]["header"]["template"] == "blue"
    title = card["card"]["header"]["title"]["content"]
    assert "诊断" in title and "内存压力" in title and "swap" in title
    body = str(card["card"]["elements"])
    assert "postgres 容器冷数据被换出" in body
    assert "docker stats --no-stream" in body
    assert "medium" in body
    assert "#7" in body  # 事件号可追溯


@pytest.mark.parametrize(
    "diagnosis",
    [
        {"summary": "只有摘要"},
        {},  # LLM 产出完全为空
        {"evidence": None, "suggested_commands": None},  # 字段存在但为 None,不可索引/迭代
        {"confidence": ""},  # 空串视同缺失
    ],
)
def test_diagnosis_card_tolerates_missing_fields(diagnosis):
    card = build_diagnosis_card(_event(), diagnosis, now_str="t")
    body = str(card["card"]["elements"])  # 任意子集都不抛异常,事件号兜底可追溯
    assert "#7" in body
    if diagnosis.get("summary"):
        assert "只有摘要" in body


def test_summary_card():
    card = build_summary_card(  # 注意:ruff 按显示宽度计长(CJK=2 列),这类行必须包裹
        "今日整体平稳。", date_str="2026-06-12", now_str="2026-06-12 09:05:00"
    )
    assert card["card"]["header"]["template"] == "green"
    assert "AI 总结" in card["card"]["header"]["title"]["content"]
    assert "今日整体平稳。" in str(card["card"]["elements"])


def test_diagnosis_card_clips_model_output():
    # 模型输出直接进卡片:超长字段钳制、列表截前 5 条,防飞书卡片超限报错/刷屏
    diagnosis = {
        "summary": "长" * 600,
        "root_cause": "因" * 600,
        "evidence": [f"证据{i}-" + "x" * 400 for i in range(10)],
        "suggested_commands": ["cmd-" + "y" * 300 for _ in range(10)],
        "confidence": "h" * 100,
    }
    card = build_diagnosis_card(_event(), diagnosis, now_str="t")
    body = str(card["card"]["elements"])
    assert "长" * 600 not in body and "长" * 400 in body  # 截断但保留前缀
    assert "证据9" not in body and "证据4" in body  # 只留前 5 条
    assert body.count("cmd-") == 5
    assert len(body) < 6000
