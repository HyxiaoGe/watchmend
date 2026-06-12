#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.27,<0.29"]
# ///
"""sentinel 恢复编排/测试入口(层 2):人工触发,起 sentinel-remediate agent 调恢复包装脚本。

用法: remediate.py restart <容器名>
飞书群 @bot 是生产主入口(经顶层 bindings 路由到 sentinel-remediate);本脚本是不依赖
建群的 dev/测试入口,走完整 脚本→agent→审批 链路。安全网是包装脚本+人工审批,非 LLM 自律。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# 复用 diag_orchestrator 的 CLI 常量与信封解析,保持单点定义(T7 改 SESSION_FLAG 只改一处)
try:
    from host.diag_orchestrator import (
        OPENCLAW_BIN,
        SESSION_FLAG,
        _run_agent_argv,
        extract_text,
    )
except ImportError:  # 部署态:同目录直跑,无 host 包
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from diag_orchestrator import (
        OPENCLAW_BIN,
        SESSION_FLAG,
        _run_agent_argv,
        extract_text,
    )

REMEDIATE_AGENT = "sentinel-remediate"
# 恢复脚本目录:部署时设 SENTINEL_RECOVERY_DIR 指向宿主机实际路径;默认=本文件同级 recovery/
RECOVERY_DIR = os.environ.get(
    "SENTINEL_RECOVERY_DIR", str(Path(__file__).resolve().parent / "recovery")
)
KNOWN_ACTIONS = {"restart": "sentinel-restart.sh"}  # 动作 → 包装脚本
TARGET_RE = re.compile(r"^[a-zA-Z0-9._-]+$")  # 目标字符集:挡 shell 元字符
AGENT_TIMEOUT_S = 60


def build_prompt(action: str, target: str) -> str:
    script = f"{RECOVERY_DIR}/{KNOWN_ACTIONS[action]}"
    return (
        "用 exec 工具执行一次恢复操作。直接以绝对路径调用下面这一条命令,"
        "不要用 shell 包装、不要加任何其他参数、不要管道或分号:\n"
        f"{script} {target}\n"
        "执行后把脚本的退出码和输出原样回报,不要再做其他事。"
    )


def run(action: str, target: str) -> int:
    if action not in KNOWN_ACTIONS:
        print(f"未知动作 '{action}',已知: {sorted(KNOWN_ACTIONS)}", file=sys.stderr)
        return 2
    if not TARGET_RE.match(target):
        print(f"非法目标 '{target}'(仅允许字母数字与 . _ -)", file=sys.stderr)
        return 2
    argv = [
        OPENCLAW_BIN,
        "agent",
        "--agent",
        REMEDIATE_AGENT,
        SESSION_FLAG,
        f"remediate-{action}-{target}",
        "--timeout",
        # agent 内层超时 60s；外层 subprocess timeout 由 diag_orchestrator 常量
        # 控制(约 360s)，远松于此——openclaw 自身 60s 内退出，对本调用安全无需调整。
        str(AGENT_TIMEOUT_S),
        "--json",
        "-m",
        build_prompt(action, target),
    ]
    try:
        result = _run_agent_argv(argv)
    except Exception as exc:
        print(f"agent 调用失败: {exc}", file=sys.stderr)
        return 1
    print(extract_text(result))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("用法: remediate.py <restart> <容器名>", file=sys.stderr)
        return 2
    return run(args[0], args[1])


if __name__ == "__main__":
    raise SystemExit(main())
