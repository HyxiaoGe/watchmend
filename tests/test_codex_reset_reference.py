import asyncio
import json

from sentinel.codex_reset.reference import ReferenceRateLimitSource, evidence_from_rate_limits


def _result(*, reset_start: int, duration: int = 10080, banked_count: int = 2):
    return {
        "rateLimitsByLimitId": {
            "codex": {
                "limitId": "codex",
                "primary": {
                    "usedPercent": 11,
                    "windowDurationMins": duration,
                    "resetsAt": reset_start + duration * 60,
                },
            }
        },
        "rateLimitResetCredits": {"availableCount": banked_count, "credits": None},
    }


def test_weekly_window_becomes_redacted_local_confirmation():
    evidence = evidence_from_rate_limits(
        _result(reset_start=1000),
        now_ts=1200,
        min_window_minutes=10000,
        max_reset_age_seconds=21600,
    )
    assert evidence is not None
    assert evidence.observed_at == 1000
    assert evidence.local_reference is True
    assert evidence.reset_type is None
    assert evidence.explicit_completed is True
    assert evidence.url == ""
    assert "11" not in evidence.summary
    assert "usedPercent" not in evidence.summary


def test_short_or_stale_windows_are_not_confirmation():
    assert (
        evidence_from_rate_limits(
            _result(reset_start=1000, duration=300),
            now_ts=1200,
            min_window_minutes=10000,
            max_reset_age_seconds=21600,
        )
        is None
    )
    assert (
        evidence_from_rate_limits(
            _result(reset_start=1000),
            now_ts=22601,
            min_window_minutes=10000,
            max_reset_age_seconds=21600,
        )
        is None
    )


class _FakeStdin:
    def __init__(self):
        self.messages = []

    def write(self, data):
        self.messages.append(json.loads(data))

    async def drain(self):
        return None


class _FakeStdout:
    def __init__(self, lines):
        self.lines = list(lines)

    async def readline(self):
        return self.lines.pop(0) if self.lines else b""


class _FakeProcess:
    def __init__(self, result):
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(
            [
                b'{"id":1,"result":{}}\n',
                json.dumps({"id": 2, "result": result}).encode() + b"\n",
            ]
        )
        self.returncode = None

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


async def test_source_uses_official_read_only_protocol_and_minimal_environment(
    tmp_path, monkeypatch
):
    codex_home = tmp_path / "readonly-codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("{}", encoding="utf-8")
    fake_processes = []
    captured = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        fake_process = _FakeProcess(_result(reset_start=1000))
        fake_processes.append(fake_process)
        return fake_process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setenv("UNRELATED_PRIVATE_VALUE", "must-not-reach-child")
    source = ReferenceRateLimitSource(
        cli_path="/usr/local/bin/codex-reference",
        codex_home=str(codex_home),
        runtime_home=str(tmp_path / "runtime-home"),
        timeout_seconds=5,
        min_window_minutes=10000,
        max_reset_age_seconds=21600,
        clock=lambda: 1200,
    )

    first = await source.fetch(None)
    fetched = await source.fetch(None)

    assert first.evidence == []
    assert fetched.name == "reference_account"
    assert fetched.content_ts == 1200
    assert fetched.evidence[0].observed_at == 1000
    assert len(fetched.banked_balances) == 1
    assert fetched.banked_balances[0].available_count == 2
    assert captured["args"] == (
        "/usr/local/bin/codex-reference",
        "app-server",
        "--listen",
        "stdio://",
    )
    assert captured["kwargs"]["stderr"] is asyncio.subprocess.DEVNULL
    assert "UNRELATED_PRIVATE_VALUE" not in captured["kwargs"]["env"]
    assert len(fake_processes) == 2
    for fake_process in fake_processes:
        assert [message.get("method") for message in fake_process.stdin.messages] == [
            "initialize",
            "initialized",
            "account/rateLimits/read",
        ]
