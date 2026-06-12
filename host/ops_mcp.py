#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["mcp>=1.4,<2"]
# ///
"""ops-mcp:只读运维查询 MCP server(stdio),供 OpenClaw sentinel-diag 诊断会话使用。

硬只读保证:所有工具都是固定 argv 的子进程调用(无 shell),用户输入仅进白名单
校验后的单一参数槽;prometheus 未发布宿主机端口,经 docker inspect 现解容器 IP。
部署:dev 宿主机 ~/sentinel-host/,由 OpenClaw mcp.servers(stdio)拉起。
"""

from __future__ import annotations

import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

from mcp.server.fastmcp import FastMCP


# 与 diag_orchestrator._load_env 同形;本脚本是 PEP723 独立脚本(依赖只有 mcp),
# import diag_orchestrator 会连带 httpx 依赖打破隔离,故内联一份
def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


_load_env(Path(__file__).resolve().parent / ".env")

mcp = FastMCP("ops")

_MAX_OUT = 8000
_TIMEOUT = 10
# 不允许以 - 开头:防止名字被解析成 docker/systemctl 的旗标
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_UNIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@_.\-]{0,254}$")


def _run(argv: list[str]) -> str:
    out = subprocess.run(argv, capture_output=True, text=True, timeout=_TIMEOUT)
    if out.returncode != 0:
        return f"exit={out.returncode}\n{out.stdout}\n{out.stderr}"[:_MAX_OUT]
    return out.stdout[:_MAX_OUT]


def _check(value: str, pattern: re.Pattern[str], what: str) -> str:
    if not pattern.match(value):
        raise ValueError(f"invalid {what}: {value!r}")
    return value


_prom_cache: dict = {"ts": 0.0, "ip": ""}


def _prom_ip() -> str:
    """prometheus 容器 IP(60s 缓存)。容器重建 IP 会变,所以现查不写死。"""
    if _prom_cache["ip"] and time.monotonic() - _prom_cache["ts"] < 60:
        return _prom_cache["ip"]
    container = os.environ.get("OPS_PROM_CONTAINER", "prometheus")
    network = os.environ.get("OPS_PROM_NETWORK", "prometheus-stack_default")
    _check(container, _NAME_RE, "container")
    _check(network, _NAME_RE, "network")  # 进 template 前必须白名单校验,防注入
    # Go template 点语法不接受带连字符的 key(评审实测报 bad character U+002D),
    # 必须用 index 写法
    fmt = f'{{{{(index .NetworkSettings.Networks "{network}").IPAddress}}}}'
    ip = _run(["docker", "inspect", "-f", fmt, container]).strip()
    if not re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
        raise RuntimeError(f"cannot resolve prometheus ip: {ip!r}")
    _prom_cache.update(ts=time.monotonic(), ip=ip)
    return ip


@mcp.tool()
def prom_query(query: str) -> str:
    """Prometheus 即时查询(PromQL)。返回原始 JSON(截断 8000 字符)。"""
    url = f"http://{_prom_ip()}:9090/api/v1/query?query=" + urllib.parse.quote(query)
    with urllib.request.urlopen(url, timeout=_TIMEOUT) as resp:  # noqa: S310 内网固定地址
        return resp.read().decode()[:_MAX_OUT]


@mcp.tool()
def docker_ps() -> str:
    """列出运行中容器(名称/状态/镜像)。"""
    return _run(["docker", "ps", "--format", "table {{.Names}}\t{{.Status}}\t{{.Image}}"])


@mcp.tool()
def docker_inspect(name: str) -> str:
    """查看单个容器完整配置(网络/挂载/limits/重启策略)。"""
    _check(name, _NAME_RE, "container name")
    return _run(["docker", "inspect", name])


@mcp.tool()
def docker_stats() -> str:
    """全部容器当前资源占用快照(CPU/内存)。"""
    return _run(
        [
            "docker",
            "stats",
            "--no-stream",
            "--format",
            "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}",
        ]
    )


@mcp.tool()
def disk_free() -> str:
    """磁盘各挂载点使用率(df -h)。"""
    return _run(["df", "-h"])


@mcp.tool()
def mem_info() -> str:
    """内存与 swap 使用(free -h)。"""
    return _run(["free", "-h"])


@mcp.tool()
def systemd_status(unit: str, user: bool = False) -> str:
    """systemd 单元状态与最近 20 行日志(只读)。user=True 查 --user 单元。"""
    _check(unit, _UNIT_RE, "unit")
    argv = ["systemctl"]
    if user:
        argv.append("--user")
    argv += ["status", unit, "--no-pager", "-n", "20"]
    return _run(argv)


if __name__ == "__main__":
    mcp.run()
