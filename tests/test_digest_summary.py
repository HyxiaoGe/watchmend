import json

from sentinel.digest_summary import summarize_digest
from sentinel.findings import DigestItem


def _item(
    id,
    text="",
    *,
    rule="log_error_new",
    container="fusion-api",
    subject=None,
    ts=1000,
    severity="warning",
    state="observed",
    payload=None,
):
    data = {"container": container, "signature": text}
    if payload is not None:
        data = payload
    return DigestItem(
        id, ts, ts, rule, subject or container, severity, state, text, json.dumps(data), 1
    )


def test_model_related_records_group_across_layers_without_claiming_incident_count():
    classes = [
        "APIConnectionError",
        "MidStreamFallbackError",
        "ModelRoundUnavailableError",
        "ModelRoundUnavailableError",
        "InternalServerError",
        "InternalServerError",
        "APIConnectionError",
    ]
    items = [
        _item(i, text, container="fusion-api" if i % 2 else "litellm-proxy", ts=1000 + i)
        for i, text in enumerate(classes, 1)
    ]
    items.append(_item(8, "Exception in ASGI application", container="agent-tools-gateway"))
    result = summarize_digest(items)
    assert result.problem_groups == 2
    assert "7 条相关记录" in result.text and "7 次故障" not in result.text
    assert "模型调用失败" in result.text and "网关应用异常" in result.text
    assert "流式" not in result.text  # 原记录没有协议错误证据，不能凭服务名补原因。
    assert "Fusion" in result.text and "LiteLLM" in result.text


def test_single_restarts_and_zero_task_shutdown_are_one_operational_summary():
    items = [
        _item(
            i,
            rule="container_restart",
            container=name,
            subject=name,
            payload={"kind": "restart", "count": 1},
        )
        for i, name in enumerate(["lingdraft", "watchmend", "uptime-kuma"], 1)
    ]
    items.append(
        _item(
            4,
            "Cancel 0 running task(s), timeout graceful shutdown exceeded",
            container="trends-mcp",
        )
    )
    result = summarize_digest(items)
    assert result.problem_groups == 0
    assert "重启记录 3 条" in result.text and "退出超时记录 1 条" in result.text
    assert "维护" not in result.text and "计划内" not in result.text
    assert len(result.body.splitlines()) == 1


def test_oom_crashloop_host_and_repeated_restart_are_not_operational_noise():
    items = [
        _item(1, rule="container_restart", subject="api", payload={"kind": "OOM", "count": 1}),
        _item(2, rule="container_crashloop", subject="worker", severity="critical"),
        _item(3, rule="container_restart", subject="host", payload={"kind": "restart", "count": 1}),
        _item(
            4, rule="container_restart", subject="repeat", payload={"kind": "restart", "count": 3}
        ),
    ]
    result = summarize_digest(items)
    assert result.problem_groups == 4
    assert "运行变更" not in result.text
    assert "worker" in result.body.splitlines()[0]


def test_distinct_unknown_errors_are_not_merged_and_raw_json_is_not_rendered():
    items = [
        _item(1, '{"msg":"ValueError","request_id":"679bce0f-903b-4894-bcf9-930e4b7095fc"}'),
        _item(2, '{"msg":"TypeError","request_id":"679bce0f-903b-4894-bcf9-930e4b7095fc"}'),
    ]
    result = summarize_digest(items)
    assert result.problem_groups == 2
    assert "ValueError" in result.text and "TypeError" in result.text
    assert "request_id" not in result.text and "679bce0f" not in result.text
    assert "Traceback" not in result.text and "观察" not in result.text


def test_group_window_crosses_clock_boundary_but_preserves_separate_periods():
    result = summarize_digest(
        [
            _item(1, "APIConnectionError", ts=1199),
            _item(2, "APIConnectionError", ts=1201),
            _item(3, "APIConnectionError", ts=1800),
        ]
    )
    assert result.problem_groups == 2
    assert "2 条相关记录" in result.text


def test_active_and_resolved_records_and_severities_remain_distinct():
    result = summarize_digest(
        [
            _item(1, "APIConnectionError", severity="warning"),
            _item(2, "APIConnectionError", severity="critical"),
            _item(3, "APIConnectionError", state="resolved"),
        ]
    )
    assert result.problem_groups == 3
    assert result.body.splitlines()[0].startswith("🔴")
    assert "已恢复" in result.body.splitlines()[-1]


def test_at_most_three_problem_rows_with_total_count_preserved():
    result = summarize_digest(
        [_item(i, f"DifferentError-{i}", container=f"service-{i}") for i in range(1, 8)]
    )
    assert result.problem_groups == 7
    assert (
        len([line for line in result.body.splitlines() if line.startswith(("🔴", "🟠", "✅"))]) == 3
    )
    assert "另有 4 组问题" in result.text


def test_nonzero_task_shutdown_and_protocol_error_remain_actionable():
    result = summarize_digest(
        [
            _item(
                1,
                "Cancel 2 running task(s), timeout graceful shutdown exceeded",
                container="trends-mcp",
            ),
            _item(
                2, "RemoteProtocolError: incomplete chunked read", container="agent-tools-gateway"
            ),
        ]
    )
    assert result.problem_groups == 2
    assert "服务退出超时" in result.text and "网关流式响应中断" in result.text
    assert "运行变更" not in result.text


def test_bad_payload_does_not_expose_long_multiline_subject():
    item = _item(1, "unknown")
    item.payload_json = "not-json"
    item.subject = '{"request_id":"679bce0f-903b-4894-bcf9-930e4b7095fc"}\n' * 100
    result = summarize_digest([item])
    assert "request_id" not in result.text and "679bce0f" not in result.text
    assert len(result.text) < 250


def test_empty_summary_has_no_claim_of_runtime_health():
    result = summarize_digest([])
    assert result.problem_groups == 0
    assert "本窗口无新增记录" in result.text


def test_rendering_does_not_mutate_source_items_or_notification_priority():
    from dataclasses import asdict

    from sentinel.notify.build import digest_notification
    from sentinel.notify.feishu_channel import render_card
    from sentinel.notify.message import Kind, Severity

    items = [_item(1, "APIConnectionError"), _item(2, "APIConnectionError")]
    before = [asdict(item) for item in items]
    n = digest_notification(items, window_label="18:00 前", now_ts=2000, now_str="t")
    card = render_card(n)
    assert n.kind is Kind.DIGEST and n.severity is Severity.INFO
    assert n.data["items"] is items and [asdict(item) for item in items] == before
    assert "2 条相关记录" in n.detail
    assert card["card"]["elements"][0]["text"]["content"].startswith("**1 组问题")
    assert "观察" not in n.detail


def test_unverified_restart_count_is_not_folded_as_single_restart():
    result = summarize_digest(
        [
            _item(1, rule="container_restart", payload={"count": True}),
            _item(2, rule="container_restart", subject="another", payload={}),
        ]
    )
    assert result.problem_groups == 2
    assert "运行变更" not in result.text
