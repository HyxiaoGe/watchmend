import json
import subprocess
from pathlib import Path
from urllib.error import HTTPError

import pytest

from sentinel.codex_notify import (
    ClientConfig,
    ConfigError,
    _parse_args,
    load_config,
    main,
    normalize_codex_event,
    run_upstream,
    send_to_watchmend,
)


def _event(**kw):
    event = {
        "type": "agent-turn-complete",
        "thread-id": "thr_123",
        "turn-id": "turn_456",
        "cwd": "/workspace/watchmend",
        "input-messages": ["接入 Codex 通知"],
        "last-assistant-message": "已完成本地修改",
    }
    event.update(kw)
    return event


def test_normalize_codex_event_builds_stable_minimal_payload_and_redacts():
    fake_api_key = "sk-" + "1234567890abcdefghijklmnop"
    payload = normalize_codex_event(
        _event(
            **{
                "input-messages": ["使用 token=abc12345 完成接入"],
                "last-assistant-message": f"结果包含 {fake_api_key}",
            }
        )
    )
    assert payload["event_id"] == "thr_123:turn_456"
    assert payload["project"] == "watchmend"
    assert "abc12345" not in payload["task_summary"]
    assert fake_api_key not in payload["result_summary"]
    assert set(payload) == {
        "event_id",
        "thread_id",
        "turn_id",
        "project",
        "cwd",
        "task_summary",
        "result_summary",
    }


def test_normalize_ignores_unsupported_events():
    assert normalize_codex_event(_event(type="approval-requested")) is None


def test_load_config_requires_private_permissions_when_token_present(tmp_path):
    path = tmp_path / "codex-notify.json"
    path.write_text(
        json.dumps({"url": "http://watchmend/notifications/codex", "token": "secret"}),
        encoding="utf-8",
    )
    path.chmod(0o644)
    with pytest.raises(ConfigError, match="权限"):
        load_config(path)
    path.chmod(0o600)
    cfg = load_config(path)
    assert cfg.url.endswith("/notifications/codex") and cfg.token == "secret"


def test_run_upstream_appends_original_event_without_shell():
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    assert run_upstream(["/bin/upstream", "turn-ended"], '{"type":"x"}', runner=runner)
    assert calls[0][0] == ["/bin/upstream", "turn-ended", '{"type":"x"}']
    assert calls[0][1]["shell"] is False


class _Response:
    status = 200

    def __init__(self, body=b'{"ok":true}'):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


def test_send_to_watchmend_retries_retryable_http_error_then_succeeds():
    calls = []

    def opener(request, timeout):
        calls.append((request, timeout))
        if len(calls) == 1:
            raise HTTPError(request.full_url, 503, "busy", {}, None)
        return _Response()

    cfg = ClientConfig("http://watchmend/notifications/codex", "secret", 1.5, 1)
    assert send_to_watchmend(_event(), cfg, opener=opener, sleeper=lambda _x: None)
    assert len(calls) == 2
    assert calls[0][0].get_header("X-watchmend-token") == "secret"


def test_send_to_watchmend_rejects_non_object_success_response():
    cfg = ClientConfig("http://watchmend/notifications/codex", "secret", 1.5, 0)
    assert not send_to_watchmend(_event(), cfg, opener=lambda *_a, **_kw: _Response(b"[]"))


def test_parse_args_does_not_consume_upstream_config_option():
    config, upstream, raw = _parse_args(
        [
            "--config",
            "/private/watchmend.json",
            "--upstream",
            "/bin/upstream",
            "--config",
            "/private/upstream.json",
            '{"type":"agent-turn-complete"}',
        ]
    )
    assert config == Path("/private/watchmend.json")
    assert upstream == ["/bin/upstream", "--config", "/private/upstream.json"]
    assert raw == '{"type":"agent-turn-complete"}'


def test_main_always_forwards_existing_callback_when_private_config_is_missing(monkeypatch):
    forwarded = []
    monkeypatch.setattr(
        "sentinel.codex_notify.run_upstream",
        lambda command, raw_event, **_kw: forwarded.append((command, raw_event)) or True,
    )
    missing = Path("/definitely/missing/codex-notify.json")
    raw = json.dumps(_event(), ensure_ascii=False)
    code = main(["--config", str(missing), "--upstream", "/bin/upstream", "turn-ended", raw])
    assert code == 0
    assert forwarded == [(["/bin/upstream", "turn-ended"], raw)]


def test_main_logs_upstream_nonzero_but_still_returns_zero(monkeypatch, caplog):
    monkeypatch.setattr("sentinel.codex_notify.run_upstream", lambda *_a, **_kw: False)
    raw = json.dumps(_event(type="approval-requested"), ensure_ascii=False)
    assert main(["--upstream", "/bin/upstream", raw]) == 0
    assert "既有 notify 回调返回失败" in caplog.text
