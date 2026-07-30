# tests/test_cards.py
from sentinel.events import EventType, TransitionEvent
from sentinel.feishu.cards import (
    build_card,
    build_daily_report_card,
    build_event_card,
    build_heartbeat_card,
    build_recovery_card,
    build_status_editor_card,
)
from sentinel.findings import EventRecord, Finding
from sentinel.models import (
    Incident,
    IncidentStatus,
    Indicator,
    ServiceDayStats,
    Snapshot,
)
from sentinel.status_editor import StatusAnalysis


def _ev(etype, impact=None):
    return TransitionEvent(
        type=etype,
        provider="openai",
        title="t",
        detail="d",
        impact=impact,
        url="https://status.openai.com",
    )


def test_card_is_v1_interactive_with_header_and_action():
    card = build_card(
        "OpenAI",
        [_ev(EventType.INCIDENT_OPENED, Indicator.MAJOR)],
        "https://status.openai.com",
        now_str="2026-06-06 12:00:00",
    )
    assert card["msg_type"] == "interactive"
    body = card["card"]
    assert "config" in body and "header" in body and "elements" in body
    action = next(element for element in body["elements"] if element["tag"] == "action")
    assert action["tag"] == "action"
    assert action["actions"][0]["url"] == "https://status.openai.com"
    assert "WatchMend" in str(body["elements"])


def test_status_editor_card_has_watchmend_brand():
    analysis = StatusAnalysis(
        decision="notify",
        severity="warning",
        headline="上游状态发生变化",
        summary="Cloudflare 正在调查异常。",
        impact_summary="可能影响边缘请求。",
        affected_services=["fusion"],
        evidence=["官方状态页已确认"],
        recommended_action="继续观察。",
        confidence=0.9,
    )
    card = build_status_editor_card(
        "Cloudflare",
        analysis,
        [_ev(EventType.INCIDENT_OPENED, Indicator.MINOR)],
        "https://status.cloudflare.com",
        now_str="2026-07-30 10:00:00",
        model="gemini/gemini-2.5-flash",
    )

    footer = card["card"]["elements"][-1]["elements"][0]["content"]
    assert footer.startswith("🤖 WatchMend · AI 辅助分析")


def test_header_color_red_for_major():
    card = build_card(
        "OpenAI", [_ev(EventType.INCIDENT_OPENED, Indicator.MAJOR)], "https://x", now_str="t"
    )
    assert card["card"]["header"]["template"] == "red"


def test_header_color_green_for_resolved_only():
    card = build_card("OpenAI", [_ev(EventType.INCIDENT_RESOLVED)], "https://x", now_str="t")
    assert card["card"]["header"]["template"] == "green"


def test_header_color_grey_for_baseline_and_meta():
    card_baseline = build_card("X", [_ev(EventType.BASELINE)], "https://x", now_str="t")
    assert card_baseline["card"]["header"]["template"] == "grey"
    card_fetch_failed = build_card("X", [_ev(EventType.FETCH_FAILED)], "https://x", now_str="t")
    assert card_fetch_failed["card"]["header"]["template"] == "grey"


def test_one_field_block_per_event():
    events = [_ev(EventType.INCIDENT_OPENED, Indicator.MAJOR), _ev(EventType.INCIDENT_UPDATED)]
    card = build_card("OpenAI", events, "https://x", now_str="t")
    divs = [e for e in card["card"]["elements"] if e.get("tag") == "div"]
    assert len(divs) == 2


def test_no_action_button_when_status_url_empty():
    from sentinel.events import EventType, TransitionEvent

    card = build_card(
        "X",
        [TransitionEvent(type=EventType.FETCH_FAILED, provider="X", title="t")],
        "",
        now_str="t",
    )
    tags = [e.get("tag") for e in card["card"]["elements"]]
    assert "action" not in tags


# ---- 心跳日报卡 ----


def _hsnap(provider, indicator=Indicator.NONE, incidents=None):
    return Snapshot(
        provider=provider,
        display_name=provider,
        indicator=indicator,
        status_url="https://x",
        components=[],
        incidents=incidents or [],
        fetched_at="2026-06-06T00:00:00Z",
    )


def _hinc(key="i1"):
    return Incident(
        key=key,
        title=key,
        status=IncidentStatus.INVESTIGATING,
        impact=Indicator.MAJOR,
        url="https://x",
        started_at=None,
        updated_at=None,
        latest_update_id="u1",
    )


def test_heartbeat_all_green_header_and_title():
    card = build_heartbeat_card(
        [_hsnap("Anthropic"), _hsnap("OpenAI", Indicator.MINOR)],
        now_str="2026-06-06 09:00",
        interval=60,
    )
    assert card["msg_type"] == "interactive"
    assert card["card"]["header"]["template"] == "green"
    assert "全部正常" in card["card"]["header"]["title"]["content"]
    body = card["card"]["elements"][0]["text"]["content"]
    # 裸 minor(无事件)也算正常 → 🟢
    assert "Anthropic 🟢" in body and "OpenAI 🟢" in body


def test_heartbeat_red_when_any_degraded():
    card = build_heartbeat_card(
        [_hsnap("Anthropic"), _hsnap("OpenAI", Indicator.MAJOR, [_hinc()])],
        now_str="t",
        interval=60,
    )
    assert card["card"]["header"]["template"] == "red"
    assert "存在异常" in card["card"]["header"]["title"]["content"]
    body = card["card"]["elements"][0]["text"]["content"]
    assert "OpenAI 🔴" in body
    summary = card["card"]["elements"][2]["text"]["content"]
    assert "1/2 正常" in summary and "1 起" in summary


def test_heartbeat_empty_snapshots_grey_not_green():
    # 空快照 ≠ 全部正常:启动初期/store 异常时灰头明示"暂无数据"
    card = build_heartbeat_card([], now_str="t", interval=60)
    assert card["card"]["header"]["template"] == "grey"
    assert "暂无数据" in card["card"]["header"]["title"]["content"]
    assert "暂无快照数据" in card["card"]["elements"][0]["text"]["content"]


# ---- 内部体检日报卡 ----


def _stats(service="auth", total=288, ok_count=288, p50=10.0, p95=20.0, baseline=None):
    return ServiceDayStats(
        service=service,
        total=total,
        ok_count=ok_count,
        p50_ms=p50,
        p95_ms=p95,
        baseline_p95_ms=baseline,
    )


def test_daily_report_all_green():
    card = build_daily_report_card(
        [_stats(), _stats(service="fusion", baseline=19.0)],
        date_str="2026-06-12",
        now_str="2026-06-12 09:00:00",
    )
    assert card["msg_type"] == "interactive"
    assert card["card"]["header"]["template"] == "green"
    assert "全部正常" in card["card"]["header"]["title"]["content"]
    body = card["card"]["elements"][0]["text"]["content"]
    assert "🟢 auth 100.0%" in body
    assert "p95 20ms" in body
    assert "→" in body  # fusion 有基线且无退化 → 平稳箭头
    summary = card["card"]["elements"][2]["text"]["content"]
    assert "2/2 服务全勤" in summary


def test_daily_report_orange_on_partial_failures():
    card = build_daily_report_card(
        [_stats(), _stats(service="loki", ok_count=287)],
        date_str="d",
        now_str="t",
    )
    assert card["card"]["header"]["template"] == "orange"
    body = card["card"]["elements"][0]["text"]["content"]
    assert "🟠 loki 99.7%" in body
    # 异常服务排前
    assert body.index("loki") < body.index("auth")


def test_daily_report_red_below_95_pct():
    card = build_daily_report_card(
        [_stats(service="search", ok_count=200)], date_str="d", now_str="t"
    )
    assert card["card"]["header"]["template"] == "red"
    assert "🔴 search 69.4%" in card["card"]["elements"][0]["text"]["content"]


def test_daily_report_orange_on_p95_degraded_vs_baseline():
    card = build_daily_report_card([_stats(p95=200.0, baseline=100.0)], date_str="d", now_str="t")
    assert card["card"]["header"]["template"] == "orange"
    assert "↑" in card["card"]["elements"][0]["text"]["content"]


def test_daily_report_zero_sample_service_is_red():
    card = build_daily_report_card(
        [_stats(service="ghost", total=0, ok_count=0, p50=None, p95=None)],
        date_str="d",
        now_str="t",
    )
    assert card["card"]["header"]["template"] == "red"
    assert "无样本" in card["card"]["elements"][0]["text"]["content"]


def test_daily_report_empty_stats_grey():
    card = build_daily_report_card([], date_str="d", now_str="t")
    assert card["card"]["header"]["template"] == "grey"
    assert "暂无探针数据" in card["card"]["elements"][0]["text"]["content"]


def test_daily_report_trend_down_on_improvement():
    card = build_daily_report_card([_stats(p95=50.0, baseline=100.0)], date_str="d", now_str="t")
    assert card["card"]["header"]["template"] == "green"
    assert "↓(基线 100ms)" in card["card"]["elements"][0]["text"]["content"]


def _finding(**kw):
    base = dict(rule="mem_pressure", subject="swap", severity="warning", detail="swap 85%")
    base.update(kw)
    return Finding(**base)


def _event(**kw):
    base = dict(
        id=1,
        ts=1000,
        rule="service_down",
        subject="auth",
        severity="critical",
        status="open",
        detail="连续 3 次失败",
        payload_json="{}",
        diagnosis_status="pending",
        diagnosis_json=None,
        cooldown_until=22600,
        resolved_ts=None,
    )
    base.update(kw)
    return EventRecord(**base)


def test_event_card_critical_is_red():
    card = build_event_card(
        _finding(rule="service_down", subject="auth", severity="critical", detail="x"),
        now_str="N",
    )
    header = card["card"]["header"]
    assert header["template"] == "red"
    assert "服务异常" in header["title"]["content"]
    assert "auth" in header["title"]["content"]


def test_event_card_warning_is_orange():
    card = build_event_card(_finding(), now_str="N")
    assert card["card"]["header"]["template"] == "orange"
    assert "内存压力" in card["card"]["header"]["title"]["content"]


def test_recovery_card_green_with_duration():
    card = build_recovery_card(_event(), now_ts=1000 + 3900, now_str="N")
    assert card["card"]["header"]["template"] == "green"
    assert "已恢复" in card["card"]["header"]["title"]["content"]
    body = card["card"]["elements"][0]["text"]["content"]
    assert "1h05m" in body
    assert "连续 3 次失败" in body  # 恢复卡带原始触发详情


def test_report_card_critical_open_event_goes_red():
    card = build_daily_report_card(
        [_stats(total=10, ok_count=10)],
        date_str="d",
        now_str="n",
        open_events=[_event()],
        resolved_24h=0,
    )
    assert card["card"]["header"]["template"] == "red"
    contents = [e["text"]["content"] for e in card["card"]["elements"] if e.get("tag") == "div"]
    assert any("未决事件 1 起" in c for c in contents)
    assert any("服务异常 · auth" in c for c in contents)


def test_report_card_warning_open_event_goes_orange():
    card = build_daily_report_card(
        [_stats(total=10, ok_count=10)],
        date_str="d",
        now_str="n",
        open_events=[_event(severity="warning", rule="mem_pressure", subject="swap")],
    )
    assert card["card"]["header"]["template"] == "orange"


def test_report_card_without_events_unchanged_green():
    card = build_daily_report_card(
        [_stats(total=10, ok_count=10)], date_str="d", now_str="n", resolved_24h=2
    )
    assert card["card"]["header"]["template"] == "green"
    contents = [e["text"]["content"] for e in card["card"]["elements"] if e.get("tag") == "div"]
    assert any("24h 内恢复 2 起" in c for c in contents)


def test_recovery_card_duration_boundaries():
    for delta, expected in [(3540, "持续 59m\n"), (3600, "持续 1h00m\n"), (-5, "持续 0m\n")]:
        card = build_recovery_card(_event(), now_ts=1000 + delta, now_str="N")
        body = card["card"]["elements"][0]["text"]["content"]
        assert expected in body, f"delta={delta}"


def test_report_card_empty_stats_stays_grey_even_with_critical_event():
    card = build_daily_report_card([], date_str="d", now_str="n", open_events=[_event()])
    assert card["card"]["header"]["template"] == "grey"
    contents = [e["text"]["content"] for e in card["card"]["elements"] if e.get("tag") == "div"]
    assert any("未决事件 1 起" in c for c in contents)
