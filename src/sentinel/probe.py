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


def load_targets(path: str) -> list[ProbeTarget]:
    """解析 services.yaml:显式清单不自动发现;url 与 host+path 二选一。"""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    defaults = data.get("defaults") or {}
    base = (data.get("nginx_base") or "http://nginx-proxy").rstrip("/")
    default_timeout = float(defaults.get("timeout", 5.0))
    default_expect = int(defaults.get("expect_status", 200))

    targets: list[ProbeTarget] = []
    for item in data["services"]:
        expect = int(item.get("expect_status", default_expect))
        timeout = float(item.get("timeout", default_timeout))
        if "url" in item:
            url, host = item["url"], None
        else:
            url, host = f"{base}{item['path']}", item["host"]
        targets.append(
            ProbeTarget(
                name=item["name"], url=url, host=host, expect_status=expect, timeout=timeout
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
