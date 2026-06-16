# src/sentinel/probe.py
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml

from sentinel.models import ProbeSample
from sentinel.store import Store


@dataclass(frozen=True)
class ProbeTarget:
    name: str
    url: str
    host: str | None = None  # 经 nginx-proxy 时的 Host 头;直连为 None
    expect_status: int = 200
    timeout: float = 5.0
    label: str | None = None  # 可选显示名;None=面板回退 name。纯展示,不参与 DB key/聚合


def load_targets(path: str) -> list[ProbeTarget]:
    """解析 services.yaml:显式清单不自动发现;url 与 host+path 二选一。"""
    # 空文件/裸 `services:`/缺 services 键都按"无内部服务"处理,回退空清单(vendor-only),
    # 不崩——这是国内新用户最易踩的坑(清空或留裸键 → 容器 crash-loop)。真正格式坏
    # (顶层标量/列表、services 写成非列表、服务项缺 name/host 等)仍响亮失败(显式配置错误)。
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    defaults = data.get("defaults") or {}
    base = (data.get("nginx_base") or "http://nginx-proxy").rstrip("/")
    default_timeout = float(defaults.get("timeout", 5.0))
    default_expect = int(defaults.get("expect_status", 200))

    # None/缺省 → 空清单(vendor-only);写成非列表标量(services: 0/true/字符串)= 类型错误,
    # 不静默吞成空,响亮失败让用户察觉(空列表 [] 正常走零次循环)。
    services = data.get("services")
    if services is None:
        services = []
    elif not isinstance(services, list):
        raise ValueError(
            f"services.yaml 'services' 必须是列表(或留空/省略),实际为 {type(services).__name__}"
        )

    targets: list[ProbeTarget] = []
    for item in services:
        expect = int(item.get("expect_status", default_expect))
        timeout = float(item.get("timeout", default_timeout))
        if "url" in item:
            url, host = item["url"], None
        else:
            url, host = f"{base}{item['path']}", item["host"]
        targets.append(
            ProbeTarget(
                name=item["name"],
                url=url,
                host=host,
                expect_status=expect,
                timeout=timeout,
                # 空串/纯空白/缺省/非字符串假值一律归一为 None(回退 name);str() 防非字符串崩 strip
                label=(str(item.get("label") or "").strip() or None),
            )
        )
    return targets


async def probe_one(client: httpx.AsyncClient, target: ProbeTarget, *, now_ts: int) -> ProbeSample:
    """单次探测:状态码 == 期望即 ok;超时/连接失败记失败样本,绝不抛异常。"""
    headers = {"Host": target.host} if target.host else {}
    start = time.monotonic()
    try:
        resp = await client.get(
            target.url, headers=headers, timeout=target.timeout, follow_redirects=False
        )
    except (httpx.HTTPError, httpx.InvalidURL):
        return ProbeSample(
            ts=now_ts, service=target.name, ok=False, status_code=None, latency_ms=None
        )
    latency_ms = round((time.monotonic() - start) * 1000, 1)
    return ProbeSample(
        ts=now_ts,
        service=target.name,
        ok=resp.status_code == target.expect_status,
        status_code=resp.status_code,
        latency_ms=latency_ms,
    )


async def run_probe_cycle(
    targets: list[ProbeTarget], *, client: httpx.AsyncClient, store: Store, now_ts: int
) -> list[ProbeSample]:
    """并发探测全部目标并落库。probe_one 自吞网络异常,gather 不会被打断。"""
    samples = list(await asyncio.gather(*(probe_one(client, t, now_ts=now_ts) for t in targets)))
    store.add_probe_samples(samples)
    return samples
