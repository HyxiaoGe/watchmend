# tests/test_app.py
import asyncio
import contextlib

import httpx
import respx


def test_health_ok(monkeypatch):
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv("SENTINEL_DB_PATH", "./data/test_app.db")
    # 不真正进入 lifespan 轮询:直接调用路由函数验证
    from sentinel.app import health

    assert health() == {"status": "ok"}


async def test_job_loop_survives_tick_crash():
    from sentinel.app import _job_loop

    calls = 0

    async def tick():
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    task = asyncio.create_task(_job_loop("t", 0.01, tick))
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert calls >= 2  # 单轮崩溃只记日志,循环继续


async def test_build_jobs_assembles_five_jobs(tmp_path, monkeypatch):
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "s.db"))
    yaml_path = tmp_path / "services.yaml"
    yaml_path.write_text(
        "services:\n  - name: auth\n    host: auth.dev.local\n    path: /health\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SENTINEL_SERVICES_FILE", str(yaml_path))

    from sentinel.app import build_jobs
    from sentinel.config import Settings
    from sentinel.store import Store

    settings = Settings(_env_file=None)
    client = httpx.AsyncClient()
    store = Store(settings.sentinel_db_path)
    jobs = build_jobs(settings, client, store)
    assert [name for name, _, _ in jobs] == [
        "statuspage",
        "internal_probe",
        "daily_report",
        "metrics_scan",
        "log_scan",
    ]
    assert [interval for _, interval, _ in jobs] == [60, 300, 60, 900, 900]
    await client.aclose()
    store.close()


async def test_probe_and_report_ticks_execute(tmp_path, monkeypatch):
    # 冒烟:真实跑一次 probe_tick 和 report_tick,钉住闭包内的 kwargs 接线
    # (_job_loop 吞所有异常,接线打错只会在生产里无限 crash-loop 而 /health 仍绿)
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "s.db"))
    yaml_path = tmp_path / "services.yaml"
    yaml_path.write_text(
        "services:\n  - name: auth\n    host: auth.dev.local\n    path: /health\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SENTINEL_SERVICES_FILE", str(yaml_path))
    monkeypatch.setenv("SENTINEL_REPORT_HOUR", "23")  # 门控大概率关着,只走查 meta 路径
    monkeypatch.setenv("SENTINEL_CERT_DOMAINS", "")  # hygiene 万一开跑也不做真实 TLS 外连
    monkeypatch.setenv("SENTINEL_BACKUP_DIR", str(tmp_path / "pg"))  # 不碰真实备份目录

    from sentinel.app import build_jobs
    from sentinel.config import Settings
    from sentinel.store import Store

    settings = Settings(_env_file=None)
    client = httpx.AsyncClient()
    store = Store(settings.sentinel_db_path)
    jobs = {name: tick for name, _, tick in build_jobs(settings, client, store)}

    with respx.mock:
        respx.get("http://nginx-proxy/health").mock(return_value=httpx.Response(200))
        await jobs["internal_probe"]()
    assert len(store.get_probe_samples_since(0)) == 1  # 闭包接线正确,样本落库

    with respx.mock:
        # 门控关 → 直接返回;万一开 → 发到 mock webhook,两种路径都不出网
        respx.post("https://open.feishu.cn/hook/T").mock(
            return_value=httpx.Response(200, json={"code": 0})
        )
        await jobs["daily_report"]()

    await client.aclose()
    store.close()


def _vector(items):
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": m, "value": [0, str(v)]} for m, v in items],
        },
    }


def _healthy_prom_handler(request):
    """必有数据的 4 个查询(disk/mem/swap/middleware)给健康值;resets/oom 空=无命中。"""
    import httpx

    q = request.url.params["query"]
    if "resets" in q or "oom" in q:
        return httpx.Response(200, json=_vector([]))
    if "pg_up" in q:
        return httpx.Response(
            200, json=_vector([({"__name__": "pg_up"}, 1), ({"__name__": "redis_up"}, 1)])
        )
    return httpx.Response(200, json=_vector([({}, 0.3)]))  # disk/mem/swap 健康水位


async def test_metrics_and_log_ticks_execute(tmp_path, monkeypatch):
    # 冒烟:钉住 metrics_tick/logs_tick 闭包接线(全健康 → 无事件无发卡)
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "s.db"))
    yaml_path = tmp_path / "services.yaml"
    yaml_path.write_text(
        "services:\n  - name: auth\n    host: auth.dev.local\n    path: /health\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SENTINEL_SERVICES_FILE", str(yaml_path))

    import httpx
    import respx

    from sentinel.app import build_jobs
    from sentinel.config import Settings
    from sentinel.store import Store

    settings = Settings(_env_file=None)
    client = httpx.AsyncClient()
    store = Store(settings.sentinel_db_path)
    jobs = {name: tick for name, _, tick in build_jobs(settings, client, store)}

    with respx.mock:
        respx.get("http://prometheus:9090/api/v1/query").mock(side_effect=_healthy_prom_handler)
        respx.get("http://loki:3100/loki/api/v1/query").mock(
            return_value=httpx.Response(200, json=_vector([]))
        )
        await jobs["metrics_scan"]()
        await jobs["log_scan"]()
    assert store.get_open_events() == []
    await client.aclose()
    store.close()


async def test_metrics_scan_consecutive_failures_escalate_to_card(tmp_path, monkeypatch):
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "s.db"))
    yaml_path = tmp_path / "services.yaml"
    yaml_path.write_text(
        "services:\n  - name: auth\n    host: auth.dev.local\n    path: /health\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SENTINEL_SERVICES_FILE", str(yaml_path))

    import json

    import httpx
    import respx

    from sentinel.app import build_jobs
    from sentinel.config import Settings
    from sentinel.store import Store

    settings = Settings(_env_file=None)
    client = httpx.AsyncClient()
    store = Store(settings.sentinel_db_path)
    jobs = {name: tick for name, _, tick in build_jobs(settings, client, store)}

    with respx.mock:
        respx.get("http://prometheus:9090/api/v1/query").mock(return_value=httpx.Response(500))
        webhook = respx.post("https://open.feishu.cn/hook/T").mock(
            return_value=httpx.Response(200, json={"code": 0})
        )
        for _ in range(3):
            await jobs["metrics_scan"]()  # tick 自吞查询异常,连续 3 次后升级发卡
    assert webhook.call_count == 1
    body = json.loads(webhook.calls.last.request.content)
    assert "Prometheus 巡检失败" in body["card"]["header"]["title"]["content"]
    opens = store.get_open_events()
    assert [(e.rule, e.subject) for e in opens] == [("scan_failed_prometheus", "metrics_scan")]
    await client.aclose()
    store.close()


async def test_log_scan_consecutive_failures_escalate_to_card(tmp_path, monkeypatch):
    # logs_tick 与 metrics_tick 是对称复制体:此测试钉住 loki 侧升级路径不被单边改坏
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "s.db"))
    yaml_path = tmp_path / "services.yaml"
    yaml_path.write_text(
        "services:\n  - name: auth\n    host: auth.dev.local\n    path: /health\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SENTINEL_SERVICES_FILE", str(yaml_path))

    import json

    import httpx
    import respx

    from sentinel.app import build_jobs
    from sentinel.config import Settings
    from sentinel.store import Store

    settings = Settings(_env_file=None)
    client = httpx.AsyncClient()
    store = Store(settings.sentinel_db_path)
    jobs = {name: tick for name, _, tick in build_jobs(settings, client, store)}

    with respx.mock:
        respx.get("http://loki:3100/loki/api/v1/query").mock(return_value=httpx.Response(500))
        webhook = respx.post("https://open.feishu.cn/hook/T").mock(
            return_value=httpx.Response(200, json={"code": 0})
        )
        for _ in range(3):
            await jobs["log_scan"]()
    assert webhook.call_count == 1
    body = json.loads(webhook.calls.last.request.content)
    assert "Loki 巡检失败" in body["card"]["header"]["title"]["content"]
    assert [(e.rule, e.subject) for e in store.get_open_events()] == [
        ("scan_failed_loki", "log_scan")
    ]
    await client.aclose()
    store.close()


async def test_open_scan_failed_event_not_falsely_recovered_after_restart(tmp_path, monkeypatch):
    # 数据源仍挂 + sentinel 重启(失败计数清零):首轮失败绝不能把 open 的巡检失败事件
    # 判恢复——计数未达阈值时 scan_failed_* 不进 scope,事件保持 open、不发假绿卡
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "s.db"))
    yaml_path = tmp_path / "services.yaml"
    yaml_path.write_text(
        "services:\n  - name: auth\n    host: auth.dev.local\n    path: /health\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SENTINEL_SERVICES_FILE", str(yaml_path))

    import httpx
    import respx

    from sentinel.app import build_jobs
    from sentinel.config import Settings
    from sentinel.store import Store

    settings = Settings(_env_file=None)
    client = httpx.AsyncClient()
    store = Store(settings.sentinel_db_path)
    store.insert_event(
        ts=1000,
        rule="scan_failed_prometheus",
        subject="metrics_scan",
        severity="warning",
        status="open",
        detail="d",
        payload_json="{}",
        diagnosis_status="skipped",
        cooldown_until=22600,
    )
    jobs = {name: tick for name, _, tick in build_jobs(settings, client, store)}

    with respx.mock:
        respx.get("http://prometheus:9090/api/v1/query").mock(return_value=httpx.Response(500))
        webhook = respx.post("https://open.feishu.cn/hook/T").mock(
            return_value=httpx.Response(200, json={"code": 0})
        )
        await jobs["metrics_scan"]()  # 重启后第 1 次失败,1 < 3
    assert webhook.call_count == 0  # 无假恢复卡
    assert [e.rule for e in store.get_open_events()] == ["scan_failed_prometheus"]
    await client.aclose()
    store.close()
