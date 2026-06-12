import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "host" / "recovery" / "sentinel-restart.sh"

# 假 docker:ps 打印固定运行容器名;restart 把参数记进日志文件;其余退 99
FAKE_DOCKER = """#!/usr/bin/env bash
if [ "$1" = "ps" ]; then
  [ "$2" = "--format" ] && [ "$3" = "{{.Names}}" ] || exit 1
  printf '%s\\n' litellm-proxy dozzle postgres watchtower
  exit 0
fi
if [ "$1" = "restart" ]; then
  shift
  [ "$1" = "--" ] && shift
  echo "restart $*" >> "$FAKE_DOCKER_LOG"
  exit 0
fi
exit 99
"""


def _run(tmp_path, args, extra_env=None):
    fake = tmp_path / "docker"
    fake.write_text(FAKE_DOCKER)
    fake.chmod(0o755)
    log = tmp_path / "calls.log"
    env = {"PATH": f"{tmp_path}:/usr/bin:/bin", "FAKE_DOCKER_LOG": str(log)}
    env.update(extra_env or {})
    proc = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env)
    calls = log.read_text().splitlines() if log.exists() else []
    return proc, calls


def test_restart_running_non_deny_container(tmp_path):
    proc, calls = _run(tmp_path, ["litellm-proxy"])
    assert proc.returncode == 0
    assert calls == ["restart litellm-proxy"]


def test_restart_stateful_container_allowed(tmp_path):
    # postgres 不在 DENY,可重启(设计:重启不丢数据,审批时自行掂量爆炸半径)
    proc, calls = _run(tmp_path, ["postgres"])
    assert proc.returncode == 0
    assert calls == ["restart postgres"]


def test_deny_container_rejected(tmp_path):
    proc, calls = _run(tmp_path, ["watchtower"])
    assert proc.returncode == 1
    assert calls == []
    assert "禁止重启" in proc.stderr


def test_deny_env_override(tmp_path):
    # 部署者经 SENTINEL_RESTART_DENY 自定义名单:命中拒绝,默认名单同时被替换
    proc, calls = _run(tmp_path, ["dozzle"], extra_env={"SENTINEL_RESTART_DENY": "dozzle postgres"})
    assert proc.returncode == 1
    assert calls == []
    proc2, calls2 = _run(
        tmp_path, ["watchtower"], extra_env={"SENTINEL_RESTART_DENY": "dozzle postgres"}
    )
    assert proc2.returncode == 0
    assert calls2 == ["restart watchtower"]


def test_nonrunning_container_rejected(tmp_path):
    proc, calls = _run(tmp_path, ["ghost-container"])
    assert proc.returncode == 1
    assert calls == []


def test_wrong_arg_count_rejected(tmp_path):
    proc2, _ = _run(tmp_path, ["a", "b"])
    assert proc2.returncode == 1
    proc0, _ = _run(tmp_path, [])
    assert proc0.returncode == 1


def test_invalid_format_target_rejected(tmp_path):
    # 含换行/空格/元字符/前导横杠的目标必须在格式校验就被拒,不触达 docker。
    # 关键用例 watchtower\nlitellm-proxy:两行都是运行容器,会绕过 grep -qxF
    # 的运行校验与 DENY 精确比对、触达 docker restart——只有格式校验能拦住它(真回归守护)。
    bad_targets = [
        "bad name",
        "-x",
        "a\nb",
        "evil;rm",
        "a/b",
        "watchtower\nlitellm-proxy",
    ]
    for bad in bad_targets:
        proc, calls = _run(tmp_path, [bad])
        assert proc.returncode == 1, f"{bad!r} 应被拒"
        assert calls == [], f"{bad!r} 不应触达 docker restart"


def test_script_has_no_dangerous_tokens():
    src = SCRIPT.read_text()
    for tok in (
        "docker rm",
        "docker stop",
        "docker down",
        "docker exec",
        "bash -c",
        "sh -c",
        "eval ",
    ):
        assert tok not in src, f"危险 token 出现在脚本: {tok!r}"
