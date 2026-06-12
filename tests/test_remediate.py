import sys

from host import remediate as r


def test_unknown_action_no_agent(monkeypatch):
    called = []
    monkeypatch.setattr(r, "_run_agent_argv", lambda argv: called.append(argv) or "")
    assert r.run("delete", "litellm-proxy") == 2
    assert called == []


def test_illegal_target_no_agent(monkeypatch):
    called = []
    monkeypatch.setattr(r, "_run_agent_argv", lambda argv: called.append(argv) or "")
    assert r.run("restart", "litellm; rm -rf /") == 2
    assert called == []


def test_known_action_builds_correct_argv(monkeypatch):
    calls = []
    monkeypatch.setattr(r, "_run_agent_argv", lambda argv: calls.append(argv) or '{"result":"ok"}')
    assert r.run("restart", "litellm-proxy") == 0
    argv = calls[0]
    assert argv[:4] == ["openclaw", "agent", "--agent", "sentinel-remediate"]
    assert "remediate-restart-litellm-proxy" in argv
    prompt = argv[-1]
    assert f"{r.RECOVERY_DIR}/sentinel-restart.sh litellm-proxy" in prompt
    assert "bash -c" not in prompt


def test_build_prompt_uses_absolute_script_path():
    p = r.build_prompt("restart", "dozzle")
    assert r.RECOVERY_DIR.startswith("/"), "默认 RECOVERY_DIR 必须是绝对路径"
    assert f"{r.RECOVERY_DIR}/sentinel-restart.sh dozzle" in p
    assert "exec" in p


def test_recovery_dir_env_override(monkeypatch):
    """部署者经 SENTINEL_RECOVERY_DIR 指定宿主机实际脚本目录(模块导入期读取)。"""
    import importlib

    monkeypatch.setenv("SENTINEL_RECOVERY_DIR", "/opt/sentinel-host/recovery")
    mod = importlib.reload(r)
    try:
        assert mod.RECOVERY_DIR == "/opt/sentinel-host/recovery"
        assert "/opt/sentinel-host/recovery/sentinel-restart.sh x" in mod.build_prompt(
            "restart", "x"
        )
    finally:
        monkeypatch.delenv("SENTINEL_RECOVERY_DIR")
        importlib.reload(r)


def test_main_wrong_argc(monkeypatch):
    monkeypatch.setattr(r, "_run_agent_argv", lambda argv: "")
    assert r.main([]) == 2
    assert r.main(["restart"]) == 2
    assert r.main(["restart", "a", "b"]) == 2


def test_empty_target_no_agent(monkeypatch):
    """M-3-1: 空字符串 target 应被 TARGET_RE 拦截，返回 2，不起 agent。"""
    called = []
    monkeypatch.setattr(r, "_run_agent_argv", lambda argv: called.append(argv) or "")
    assert r.run("restart", "") == 2
    assert called == []


def test_target_with_dot_allowed(monkeypatch):
    """M-3-2: 含点号的合法名应通过 TARGET_RE，agent 被调用，返回 0。"""
    calls = []
    monkeypatch.setattr(r, "_run_agent_argv", lambda argv: calls.append(argv) or '{"result":"ok"}')
    monkeypatch.setattr(r, "extract_text", lambda x: "ok")
    assert r.run("restart", "a.b") == 0
    assert calls != []


def test_main_none_reads_sys_argv(monkeypatch):
    """M-3-3: main(None) 应从 sys.argv[1:] 读取参数，正常路径返回 0。"""
    monkeypatch.setattr(sys, "argv", ["remediate.py", "restart", "x"])
    monkeypatch.setattr(r, "_run_agent_argv", lambda argv: '{"result":"ok"}')
    monkeypatch.setattr(r, "extract_text", lambda x: "ok")
    assert r.main(None) == 0
