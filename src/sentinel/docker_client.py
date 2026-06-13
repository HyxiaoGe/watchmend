"""共享只读 Docker 访问层(Engine API over UDS 或 TCP proxy)。

LLMDriver 的诊断工具与 scan_docker 的检测层共用一份连接。只暴露只读 GET;
inspect 走白名单提取(密钥/宿主路径整段丢弃)后才进 payload/发 LLM。
端点 tcp:// 必须转 http:// 才能喂 httpx(否则 UnsupportedProtocol)。
"""

from __future__ import annotations

import re
from datetime import datetime

import httpx

# 不允许以 - 开头:防止名字被当成旗标/路径段注入(同 ops_mcp 的纪律)
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _check_name(name: str) -> None:
    if not _NAME_RE.match(name):
        raise ValueError(f"invalid container name: {name!r}")


def _demux_docker_logs(raw: bytes) -> str:
    """Engine API 日志流:TTY 关闭时是 8 字节帧头多路流,开启时是裸流。
    帧头不合形就整体按裸流退回,不让二进制头混进给模型的文本。"""
    out: list[bytes] = []
    i = 0
    try:
        while i + 8 <= len(raw):
            if raw[i] not in (0, 1, 2) or raw[i + 1 : i + 4] != b"\x00\x00\x00":
                raise ValueError("not a multiplexed stream")
            size = int.from_bytes(raw[i + 4 : i + 8], "big")
            out.append(raw[i + 8 : i + 8 + size])
            i += 8 + size
        return b"".join(out).decode("utf-8", errors="replace")
    except ValueError:
        return raw.decode("utf-8", errors="replace")


def parse_docker_time(s: str) -> int | None:
    """RFC3339Nano → epoch 秒。零值('0001-01-01...')/空/解析失败 → None
    (调用方按'未知'保守处理)。Docker 发 9 位纳秒,先把纳秒截到微秒、末尾 Z 换
    +00:00 再喂 fromisoformat(防御性:对老版本 Python 也成立)。"""
    if not s or s.startswith("0001-01-01"):
        return None
    s = re.sub(r"(\.\d{6})\d+", r"\1", s).replace("Z", "+00:00")
    try:
        return int(datetime.fromisoformat(s).timestamp())
    except ValueError:
        return None


class DockerClient:
    """只读 Engine API 访问层。endpoint 空时上层应不构造本类。"""

    def __init__(self, endpoint: str, *, timeout: float = 15.0) -> None:
        scheme, _, rest = endpoint.partition("://")  # 关键:不能原样喂 httpx
        if scheme == "unix":
            transport: httpx.AsyncHTTPTransport | None = httpx.AsyncHTTPTransport(uds=rest)
            base_url = "http://docker"  # UDS 下 host 仅占位
        elif scheme in ("tcp", "http", "https"):
            transport = None
            base_url = f"http://{rest}"  # tcp://proxy:2375 → http://proxy:2375
        else:
            raise ValueError(f"unsupported docker endpoint: {endpoint!r}")
        self._client = httpx.AsyncClient(transport=transport, base_url=base_url, timeout=timeout)

    async def ps(self, *, all: bool = True) -> list[dict]:
        """GET /containers/json(检测层必须 all=1,才能看到 exited)。"""
        params = {"all": "1"} if all else {}
        resp = await self._client.get("/containers/json", params=params)
        resp.raise_for_status()
        return resp.json()

    async def inspect_safe(self, name: str) -> dict:
        """GET /containers/{name}/json,白名单提取后返回。
        Args/Binds/Mounts.Source/NetworkSettings 整段丢弃,Env 仅留变量名;
        Health 只取 Status——Health.Log[].Output 是健康检查命令的原始输出,
        常含密钥/连接串/宿主路径,绝不进 payload。"""
        _check_name(name)
        resp = await self._client.get(f"/containers/{name}/json")
        resp.raise_for_status()
        data = resp.json()
        config = data.get("Config") or {}
        state = data.get("State") or {}
        host = data.get("HostConfig") or {}
        health = state.get("Health")
        return {
            "Name": data.get("Name"),
            "Image": config.get("Image"),
            "RestartCount": data.get("RestartCount"),
            "RestartPolicy": (host.get("RestartPolicy") or {}).get("Name"),
            "State": {
                "Status": state.get("Status"),
                "Running": state.get("Running"),
                "Restarting": state.get("Restarting"),
                "ExitCode": state.get("ExitCode"),
                "OOMKilled": state.get("OOMKilled"),
                "Error": state.get("Error"),
                "StartedAt": state.get("StartedAt"),
                "FinishedAt": state.get("FinishedAt"),
                "Health": {"Status": health.get("Status")} if isinstance(health, dict) else None,
            },
            "Env": [str(e).split("=", 1)[0] for e in (config.get("Env") or [])],
        }

    async def logs(self, name: str, *, tail: int = 100) -> str:
        """GET /containers/{name}/logs(stdout+stderr)+ demux。tail 钳到 [1,500]。"""
        _check_name(name)
        tail = max(1, min(int(tail), 500))
        resp = await self._client.get(
            f"/containers/{name}/logs",
            params={"stdout": "true", "stderr": "true", "tail": str(tail)},
        )
        resp.raise_for_status()
        return _demux_docker_logs(resp.content)

    async def aclose(self) -> None:
        await self._client.aclose()
