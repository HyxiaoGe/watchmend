import asyncio
import json

from sentinel.codex_hooks import CodexHookEvent, CodexHookNotificationManager
from sentinel.codex_notify import (
    ClientConfig,
    hook_main,
    normalize_codex_hook_event,
    send_hook_to_watchmend,
)
from sentinel.notify.message import Kind
from sentinel.store import Store


class _Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _Broadcaster:
    def __init__(self) -> None:
        self.sent = []

    async def send(self, notification) -> int:
        self.sent.append(notification)
        return 1


def _hook(name: str, **overrides) -> CodexHookEvent:
    body = {
        "event_id": f"session-1:turn-1:{name}",
        "event_name": name,
        "session_id": "session-1",
        "turn_id": "turn-1",
        "project": "watchmend",
        "cwd": "/workspace/watchmend",
    }
    body.update(overrides)
    return CodexHookEvent.model_validate(body)


def _manager(tmp_path, clock: _Clock):
    store = Store(str(tmp_path / "hooks.db"))
    broadcaster = _Broadcaster()
    manager = CodexHookNotificationManager(
        store=store,
        broadcaster=broadcaster,
        grace_seconds=300,
        long_turn_seconds=180,
        utc_offset=8,
        clock=clock,
    )
    return manager, store, broadcaster


def test_normalize_hook_event_only_sends_allowlisted_redacted_fields():
    fake_api_key = "sk-" + "1234567890abcdefghijklmnop"
    payload = normalize_codex_hook_event(
        {
            "hook_event_name": "PermissionRequest",
            "session_id": "session-1",
            "turn_id": "turn-1",
            "cwd": "/workspace/watchmend",
            "model": "gpt-5",
            "permission_mode": "default",
            "tool_name": "Bash",
            "tool_input": {
                "command": f"curl -H 'Authorization: Bearer {fake_api_key}' example.com",
                "description": "部署到 dev",
            },
            "tool_response": {"private": "不能发送"},
        }
    )

    assert payload is not None
    assert payload["event_name"] == "PermissionRequest"
    assert payload["project"] == "watchmend"
    assert payload["result_summary"] == "等待审批：部署到 dev"
    assert len(payload["tool_fingerprint"]) == 64
    assert fake_api_key not in json.dumps(payload, ensure_ascii=False)
    assert "tool_input" not in payload and "tool_response" not in payload


def test_normalize_hook_event_ignores_unknown_event():
    assert normalize_codex_hook_event({"hook_event_name": "PreCompact"}) is None


def test_hook_main_reads_stdin_posts_hook_endpoint_and_returns_neutral_json(monkeypatch, capsys):
    sent = []
    config = ClientConfig("http://watchmend/notifications/codex", "secret", 4.0, 1)
    raw = json.dumps(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-1",
            "turn_id": "turn-1",
            "cwd": "/workspace/watchmend",
            "prompt": "继续处理",
        },
        ensure_ascii=False,
    )
    monkeypatch.setattr("sentinel.codex_notify.load_config", lambda _path: config)
    monkeypatch.setattr(
        "sentinel.codex_notify.send_hook_to_watchmend",
        lambda payload, cfg: sent.append((payload, cfg)) or True,
    )
    monkeypatch.setattr("sys.stdin.read", lambda _limit: raw)

    assert hook_main(["--config", "/private/codex-notify.json"]) == 0
    assert sent[0][0]["event_name"] == "UserPromptSubmit"
    assert sent[0][0]["task_summary"] == "继续处理"
    assert capsys.readouterr().out.strip() == "{}"


def test_send_hook_uses_derived_endpoint_short_timeout_and_no_retry():
    calls = []

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"ok":true}'

    def opener(request, timeout):
        calls.append((request, timeout))
        return _Response()

    config = ClientConfig("http://watchmend/notifications/codex", "secret", 4.0, 3)
    assert send_hook_to_watchmend({"event_name": "Stop"}, config, opener=opener)
    assert calls[0][0].full_url == "http://watchmend/notifications/codex/hooks"
    assert calls[0][1] == 1.5


async def test_permission_is_sent_only_after_grace_period(tmp_path):
    clock = _Clock()
    manager, store, broadcaster = _manager(tmp_path, clock)
    try:
        result = manager.ingest(
            _hook(
                "PermissionRequest",
                result_summary="等待审批：部署到 dev",
                tool_name="Bash",
                tool_fingerprint="a" * 64,
            )
        )
        assert result == {"action": "queued", "category": "approval_required"}

        clock.now += 299
        assert await manager.dispatch_due() == 0
        clock.now += 1
        assert await manager.dispatch_due() == 1
        assert broadcaster.sent[0].kind is Kind.CODEX_TURN
        assert broadcaster.sent[0].data["category"] == "approval_required"
    finally:
        store.close()


async def test_post_tool_use_cancels_approval_before_delivery(tmp_path):
    clock = _Clock()
    manager, store, broadcaster = _manager(tmp_path, clock)
    try:
        manager.ingest(
            _hook(
                "PermissionRequest",
                result_summary="等待审批：运行测试",
                tool_name="Bash",
                tool_fingerprint="a" * 64,
            )
        )
        clock.now += 120
        result = manager.ingest(
            _hook(
                "PostToolUse",
                event_id="session-1:turn-1:PostToolUse:tool-1",
                tool_name="Bash",
                tool_use_id="tool-1",
                tool_fingerprint="a" * 64,
            )
        )
        assert result == {"action": "cancelled", "count": 1}

        clock.now += 300
        assert await manager.dispatch_due() == 0
        assert broadcaster.sent == []
    finally:
        store.close()


async def test_user_prompt_cancels_pending_input_notification(tmp_path):
    clock = _Clock()
    manager, store, broadcaster = _manager(tmp_path, clock)
    try:
        manager.ingest(
            _hook(
                "Stop",
                result_summary="请提供目标环境后我再继续。",
            )
        )
        clock.now += 240
        result = manager.ingest(
            _hook(
                "UserPromptSubmit",
                event_id="session-1:turn-2:UserPromptSubmit",
                turn_id="turn-2",
                task_summary="部署到 dev",
            )
        )
        assert result == {"action": "cancelled", "count": 1}

        clock.now += 120
        assert await manager.dispatch_due() == 0
        assert broadcaster.sent == []
    finally:
        store.close()


async def test_long_turn_completion_is_queued_but_short_success_is_ignored(tmp_path):
    clock = _Clock()
    manager, store, broadcaster = _manager(tmp_path, clock)
    try:
        manager.ingest(_hook("UserPromptSubmit", task_summary="实现通知"))
        clock.now += 60
        result = manager.ingest(_hook("Stop", result_summary="已完成修改并通过测试。"))
        assert result == {"action": "ignored", "reason": "short_success"}

        manager.ingest(
            _hook(
                "UserPromptSubmit",
                event_id="session-1:turn-2:UserPromptSubmit",
                turn_id="turn-2",
                task_summary="部署并回归",
            )
        )
        clock.now += 180
        result = manager.ingest(
            _hook(
                "Stop",
                event_id="session-1:turn-2:Stop",
                turn_id="turn-2",
                result_summary="已完成部署并通过回归。",
            )
        )
        assert result == {"action": "queued", "category": "long_turn_complete"}

        clock.now += 300
        assert await manager.dispatch_due() == 1
        assert broadcaster.sent[0].data["task_summary"] == "部署并回归"
    finally:
        store.close()


async def test_failed_or_blocked_stop_is_queued_and_duplicate_does_not_delay_it(tmp_path):
    clock = _Clock()
    manager, store, broadcaster = _manager(tmp_path, clock)
    try:
        event = _hook("Stop", result_summary="部署失败，无法继续，请检查远端权限。")
        assert manager.ingest(event) == {"action": "queued", "category": "input_required"}
        clock.now += 200
        assert manager.ingest(event) == {"action": "duplicate"}
        clock.now += 100
        assert await manager.dispatch_due() == 1
        assert broadcaster.sent[0].data["category"] == "input_required"
    finally:
        store.close()


async def test_session_end_counts_as_user_operation_and_cancels_pending(tmp_path):
    clock = _Clock()
    manager, store, broadcaster = _manager(tmp_path, clock)
    try:
        manager.ingest(_hook("Stop", result_summary="测试失败，当前任务已被阻塞。"))
        clock.now += 30
        result = manager.ingest(
            _hook(
                "SessionEnd",
                event_id="session-1:SessionEnd",
                turn_id=None,
            )
        )
        assert result == {"action": "cancelled", "count": 1}
        clock.now += 300
        assert await manager.dispatch_due() == 0
    finally:
        store.close()


async def test_user_rejected_approval_does_not_create_a_new_stop_notification(tmp_path):
    clock = _Clock()
    manager, store, broadcaster = _manager(tmp_path, clock)
    try:
        manager.ingest(
            _hook(
                "PermissionRequest",
                result_summary="等待审批：部署到 dev",
                tool_name="Bash",
                tool_fingerprint="a" * 64,
            )
        )
        clock.now += 30
        result = manager.ingest(
            _hook(
                "Stop",
                result_summary="用户拒绝了本次执行授权。",
            )
        )
        assert result == {"action": "ignored", "reason": "not_actionable"}
        clock.now += 300
        assert await manager.dispatch_due() == 0
        assert broadcaster.sent == []
    finally:
        store.close()


async def test_user_activity_during_failed_delivery_prevents_retry(tmp_path):
    clock = _Clock()
    store = Store(str(tmp_path / "inflight.db"))
    started = asyncio.Event()
    release = asyncio.Event()

    class _SlowFailingBroadcaster:
        async def send(self, _notification) -> int:
            started.set()
            await release.wait()
            return 0

    manager = CodexHookNotificationManager(
        store=store,
        broadcaster=_SlowFailingBroadcaster(),
        grace_seconds=300,
        long_turn_seconds=180,
        utc_offset=8,
        clock=clock,
    )
    try:
        manager.ingest(_hook("Stop", result_summary="测试失败，当前任务已被阻塞。"))
        clock.now += 300
        dispatch = asyncio.create_task(manager.dispatch_due())
        await started.wait()
        result = manager.ingest(
            _hook(
                "UserPromptSubmit",
                event_id="session-1:turn-2:UserPromptSubmit",
                turn_id="turn-2",
                task_summary="继续修复",
            )
        )
        assert result == {"action": "cancelled", "count": 1}
        release.set()
        assert await dispatch == 0
        assert manager.pending_count == 0
    finally:
        store.close()
