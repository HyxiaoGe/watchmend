import json

import httpx
import pytest
import respx

from host import diag_orchestrator as d

EVENT = {
    "id": 5,
    "ts": 1000,
    "rule": "mem_pressure",
    "subject": "swap",
    "severity": "warning",
    "status": "open",
    "detail": "swap 83.4% > 80%",
    "payload_json": json.dumps({"pct": 83.4}),
    "diagnosis_status": "pending",
    "diagnosis_json": None,
    "cooldown_until": 0,
    "resolved_ts": None,
}
GOOD = json.dumps(
    {"result": '```json\n{"summary": "s", "root_cause": "r", "confidence": "high"}\n```'}
)


@pytest.fixture
def base(monkeypatch):
    monkeypatch.setattr(d, "SENTINEL_URL", "http://sentinel")
    monkeypatch.setattr(d, "TOKEN", "sek")
    return d


def _mock_agent(monkeypatch, outputs: list):
    calls: list[list[str]] = []

    def fake(argv):
        calls.append(argv)
        out = outputs[min(len(calls) - 1, len(outputs) - 1)]
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(d, "_run_agent_argv", fake)
    return calls


@respx.mock
def test_happy_path_posts_done(base, monkeypatch):
    respx.get("http://sentinel/events/pending").mock(
        return_value=httpx.Response(200, json={"events": [EVENT]})
    )
    post = respx.post("http://sentinel/events/5/diagnosis").mock(
        return_value=httpx.Response(200, json={"ok": True, "card_sent": True})
    )
    calls = _mock_agent(monkeypatch, [GOOD])
    assert base.main() == 0
    argv = calls[0]
    assert argv[:3] == ["openclaw", "agent", "--agent"]
    assert "sentinel-diag" in argv and "diag-evt-5" in argv
    body = json.loads(post.calls[0].request.content)
    assert body["status"] == "done"
    assert body["diagnosis"]["root_cause"] == "r"
    assert post.calls[0].request.headers["x-sentinel-token"] == "sek"


@respx.mock
def test_retry_then_failed(base, monkeypatch):
    respx.get("http://sentinel/events/pending").mock(
        return_value=httpx.Response(200, json={"events": [EVENT]})
    )
    post = respx.post("http://sentinel/events/5/diagnosis").mock(
        return_value=httpx.Response(200, json={"ok": True, "card_sent": False})
    )
    calls = _mock_agent(monkeypatch, ["not json at all"])
    assert base.main() == 0
    assert len(calls) == 2  # 重试 1 次
    body = json.loads(post.calls[0].request.content)
    assert body["status"] == "failed"
    assert "raw" in body["diagnosis"]


@respx.mock
def test_empty_pending_zero_agent_calls(base, monkeypatch):
    respx.get("http://sentinel/events/pending").mock(
        return_value=httpx.Response(200, json={"events": []})
    )
    calls = _mock_agent(monkeypatch, [GOOD])
    assert base.main() == 0
    assert calls == []


@respx.mock
def test_caps_at_three_events(base, monkeypatch):
    events = [dict(EVENT, id=i) for i in range(1, 6)]
    respx.get("http://sentinel/events/pending").mock(
        return_value=httpx.Response(200, json={"events": events})
    )
    respx.post(url__regex=r"http://sentinel/events/\d+/diagnosis").mock(
        return_value=httpx.Response(200, json={"ok": True, "card_sent": True})
    )
    calls = _mock_agent(monkeypatch, [GOOD])
    base.main()
    assert len(calls) == 3


def test_parse_diagnosis_variants():
    fenced = '前置说明\n```json\n{"summary": "a"}\n```\n后缀'
    assert d.parse_diagnosis(fenced) == {"summary": "a"}
    bare = '说两句 {"summary": "b", "evidence": ["x"]} 结尾'
    assert d.parse_diagnosis(bare)["summary"] == "b"
    assert d.parse_diagnosis("没有 json") is None


def test_extract_text_from_envelope():
    assert d.extract_text(GOOD).startswith("```json")
    assert d.extract_text(json.dumps({"text": "T"})) == "T"
    assert d.extract_text("plain") == "plain"
    payloads = json.dumps({"payloads": [{"text": "A"}, {"text": "B"}], "result": {"x": 1}})
    assert d.extract_text(payloads) == "A\nB"  # payloads 优先于非字符串 result


def test_parse_diagnosis_rejects_shapeless_json():
    # 信封/杂散 JSON 不含 summary/root_cause → 必须判失败,绝不能当诊断回写 done
    assert d.parse_diagnosis('{"result": "junk", "meta": {}}') is None


# T7 冒烟实测信封(2026.6.5):payloads 嵌在 result 里,顶层是 runId/status/summary
REAL_ENVELOPE = json.dumps(
    {
        "result": {
            "payloads": [
                {"text": '```json\n{"summary": "s2", "root_cause": "r2"}\n```', "mediaUrl": None}
            ],
            "meta": {"durationMs": 9985},
        },
        "runId": "run-1",
        "status": "ok",
        "summary": "agent run summary",
    }
)


def test_extract_text_from_real_nested_envelope():
    text = d.extract_text(REAL_ENVELOPE)
    assert text.startswith("```json")
    assert d.parse_diagnosis(text) == {"summary": "s2", "root_cause": "r2"}


def test_parse_diagnosis_rejects_raw_envelope():
    # 信封顶层恰好有 summary 键:整个信封绝不能被当成诊断回写 done
    assert d.parse_diagnosis(REAL_ENVELOPE) is None


def test_prompt_contains_event_and_rules():
    p = d.build_prompt(EVENT)
    assert "swap 83.4% > 80%" in p and "mem_pressure" in p
    assert "只读" in p and "json" in p.lower()
