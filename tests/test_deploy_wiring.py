# tests/test_deploy_wiring.py
"""守护 P1 部署接线:llm.yaml 必须挂进容器,否则 host 的 `make llm-*` 改动不生效。

Codex P1 回归防护:host CLI 写 ./llm.yaml,容器经 bind-mount 读同一文件;
make up/demo 先脚手架出占位文件(否则 docker 对缺失 bind 源会误建空目录)。
"""

import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
LLM_MOUNT = "./llm.yaml:/app/llm.yaml:ro"


def _sentinel_volumes(compose_file: str) -> list[str]:
    spec = yaml.safe_load((ROOT / compose_file).read_text(encoding="utf-8"))
    return spec["services"]["sentinel"]["volumes"]


def test_compose_mounts_llm_yaml():
    assert LLM_MOUNT in _sentinel_volumes("docker-compose.yml")


def test_demo_compose_mounts_llm_yaml():
    assert LLM_MOUNT in _sentinel_volumes("docker-compose.demo.yml")


def test_makefile_scaffolds_llm_yaml_before_up_and_demo():
    mk = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "llm-yaml-ready:" in mk  # 有脚手架目标
    up_line = next(ln for ln in mk.splitlines() if ln.startswith("up:"))
    demo_line = next(ln for ln in mk.splitlines() if ln.startswith("demo:"))
    assert "llm-yaml-ready" in up_line  # up 依赖它,否则容器看不到 host 改动
    assert "llm-yaml-ready" in demo_line


# ---- 渠道门禁:Telegram 需 token+chat_id 成对(config.telegram_enabled=bool(token and chat_id))----
# 回归:门禁若把 BOT_TOKEN 当单钥渠道,token-only 的 .env 能过 make up,容器却 crash-loop。


def _channel_gate_blocks() -> list[str]:
    """从 Makefile 抽出「未配置任何通知渠道」那条 recipe(可能 \\ 多行续),还原成可跑 shell。

    up/demo 各一处 → 返回两块。去掉 recipe 的 @ 静默前缀,\\<newline> 交给 sh 续行。
    """
    lines = (ROOT / "Makefile").read_text(encoding="utf-8").splitlines()
    blocks: list[str] = []
    for i, ln in enumerate(lines):
        if "未配置任何通知渠道" not in ln:  # 门禁 echo 独有,避开注释/脚手架同义句
            continue
        start = i
        while start > 0 and lines[start - 1].rstrip().endswith("\\"):
            start -= 1
        end = i
        while lines[end].rstrip().endswith("\\"):
            end += 1
        block = "\n".join(lines[start : end + 1]).strip()
        blocks.append(re.sub(r"^@", "", block))  # 去掉 Make 静默前缀
    return blocks


def _run_gate(block: str, env_text: str, tmp_path: Path) -> int:
    (tmp_path / ".env").write_text(env_text, encoding="utf-8")
    return subprocess.run(
        ["sh", "-c", block], cwd=tmp_path, capture_output=True, text=True
    ).returncode


def test_channel_gate_rejects_telegram_token_without_chat_id(tmp_path):
    blocks = _channel_gate_blocks()
    assert len(blocks) == 2, "up + demo 两处渠道门禁都该抽到"
    for block in blocks:
        rc = _run_gate(block, "SENTINEL_TELEGRAM_BOT_TOKEN=fake-token\n", tmp_path)
        assert rc == 1, "只配 BOT_TOKEN(缺 CHAT_ID)应被门禁拒,否则容器会 crash-loop"


def test_channel_gate_accepts_telegram_token_with_chat_id(tmp_path):
    blocks = _channel_gate_blocks()
    for block in blocks:
        rc = _run_gate(
            block, "SENTINEL_TELEGRAM_BOT_TOKEN=fake\nSENTINEL_TELEGRAM_CHAT_ID=123\n", tmp_path
        )
        assert rc == 0, "token+chat_id 成对应放行"


def test_channel_gate_accepts_single_key_channels(tmp_path):
    blocks = _channel_gate_blocks()
    for block in blocks:
        assert _run_gate(block, "FEISHU_VENDOR_WEBHOOK=https://open.feishu.cn/h/T\n", tmp_path) == 0
        assert _run_gate(block, "SENTINEL_NTFY_URL=https://ntfy.sh/topic\n", tmp_path) == 0


def test_channel_gate_rejects_empty(tmp_path):
    blocks = _channel_gate_blocks()
    for block in blocks:
        assert _run_gate(block, "SENTINEL_DB_PATH=/data/x.db\n", tmp_path) == 1
