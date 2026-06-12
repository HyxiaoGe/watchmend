import json
import subprocess

import httpx
import pytest
import respx

from host import daily_summary as ds

DATA = {
    "date": "2026-06-12",
    "services": [
        {
            "service": "auth",
            "total": 288,
            "ok_count": 288,
            "p50_ms": 12.0,
            "p95_ms": 40.0,
            "baseline_p95_ms": 38.0,
        }
    ],
    "open_events": [],
    "resolved_24h": 1,
}


@pytest.fixture
def base(monkeypatch):
    monkeypatch.setattr(ds, "SENTINEL_URL", "http://sentinel")
    monkeypatch.setattr(ds, "TOKEN", "sek")
    return ds


@respx.mock
def test_happy_path(base, monkeypatch):
    respx.get("http://sentinel/report/daily-data").mock(return_value=httpx.Response(200, json=DATA))
    post = respx.post("http://sentinel/report/summary").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    monkeypatch.setattr(
        ds, "_run_agent_argv", lambda argv: json.dumps({"result": "一切平稳，无异常。"})
    )
    assert base.main() == 0
    body = json.loads(post.calls[0].request.content)
    assert body["text"] == "一切平稳，无异常。"


@respx.mock
def test_agent_failure_returns_nonzero(base, monkeypatch):
    respx.get("http://sentinel/report/daily-data").mock(return_value=httpx.Response(200, json=DATA))
    monkeypatch.setattr(ds, "_run_agent_argv", lambda argv: "")
    assert base.main() == 1  # 非零退出 → openclaw cron 失败通知


@respx.mock
@pytest.mark.parametrize(
    "stdout",
    [
        "Error: unknown flag --json\n",  # CLI 报错文本直出 stdout(非 JSON)
        json.dumps({"meta": {"v": 1}, "data": "总结在不认识的键里"}),  # 信封形状不认识
    ],
)
def test_unrecognized_stdout_fails_loudly_without_posting(base, monkeypatch, capsys, stdout):
    respx.get("http://sentinel/report/daily-data").mock(return_value=httpx.Response(200, json=DATA))
    post = respx.post("http://sentinel/report/summary").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    monkeypatch.setattr(ds, "_run_agent_argv", lambda argv: stdout)
    assert base.main() == 1  # 响亮失败进 cron 通知,绝不把原始 stdout 发成公开卡
    assert not post.called
    assert "unrecognized agent output" in capsys.readouterr().err


def test_run_agent_argv_reports_stderr_on_empty_stdout(monkeypatch, capsys):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode=2, stdout="", stderr="boom: bad flag")

    monkeypatch.setattr(ds.subprocess, "run", fake_run)
    # 排障信息只进本进程 stderr,绝不混入返回值(返回值会被发成总结卡)
    assert ds._run_agent_argv(["openclaw"]) == ""
    err = capsys.readouterr().err
    assert "exit=2" in err and "boom: bad flag" in err


def test_prompt_embeds_data(base):
    p = ds.build_prompt(DATA)
    assert "auth" in p and "2026-06-12" in p and "2-3" in p


def test_prompt_guides_systemic_correlation(base):
    # 总结应被引导:多服务同向变化→优先怀疑宿主机共享瓶颈并与资源事件关联,
    # 不要把各服务当独立问题孤立罗列(避免今早"auth 延迟+swap"被并列成两件事的偏差)
    p = ds.build_prompt(DATA)
    assert "共享瓶颈" in p
    assert "孤立" in p
