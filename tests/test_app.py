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
    monkeypatch.setenv("SENTINEL_PROMETHEUS_URL", "http://prometheus:9090")
    monkeypatch.setenv("SENTINEL_LOKI_URL", "http://loki:3100")

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


async def test_build_jobs_minimal_mode_vendor_only(tmp_path, monkeypatch):
    # 最小模式:只填飞书 webhook——services 文件缺失 + prometheus/loki 留空,
    # 只剩外部状态页轮询与日报门控,不产生任何数据源故障噪音
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("SENTINEL_SERVICES_FILE", str(tmp_path / "no-such.yaml"))
    monkeypatch.setenv("SENTINEL_PROMETHEUS_URL", "")
    monkeypatch.setenv("SENTINEL_LOKI_URL", "")

    from sentinel.app import build_jobs
    from sentinel.config import Settings
    from sentinel.store import Store

    settings = Settings(_env_file=None)
    client = httpx.AsyncClient()
    store = Store(settings.sentinel_db_path)
    jobs = build_jobs(settings, client, store)
    assert [name for name, _, _ in jobs] == ["statuspage", "daily_report"]
    await client.aclose()
    store.close()


async def test_build_jobs_partial_datasources(tmp_path, monkeypatch):
    # 只接 prometheus 不接 loki:metrics_scan 在、log_scan 不在
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "s.db"))
    yaml_path = tmp_path / "services.yaml"
    yaml_path.write_text(
        "services:\n  - name: auth\n    host: auth.dev.local\n    path: /health\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SENTINEL_SERVICES_FILE", str(yaml_path))
    monkeypatch.setenv("SENTINEL_PROMETHEUS_URL", "http://prometheus:9090")
    monkeypatch.setenv("SENTINEL_LOKI_URL", "")

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
    ]
    await client.aclose()
    store.close()


def test_load_targets_or_disable_missing_file_returns_empty(tmp_path):
    from sentinel.app import _load_targets_or_disable

    assert _load_targets_or_disable(str(tmp_path / "nope.yaml")) == []


def test_load_targets_or_disable_bad_yaml_still_raises(tmp_path):
    # 文件存在但内容坏=显式配置错误,必须响亮失败而非静默降级
    import pytest

    from sentinel.app import _load_targets_or_disable

    bad = tmp_path / "bad.yaml"
    bad.write_text("defaults: {}\n", encoding="utf-8")  # 缺 services 键
    with pytest.raises(KeyError):
        _load_targets_or_disable(str(bad))


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
    monkeypatch.setenv("SENTINEL_PROMETHEUS_URL", "http://prometheus:9090")
    monkeypatch.setenv("SENTINEL_LOKI_URL", "http://loki:3100")

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
    monkeypatch.setenv("SENTINEL_PROMETHEUS_URL", "http://prometheus:9090")

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


def test_load_targets_or_disable_directory_returns_empty(tmp_path):
    # compose 短语法 bind mount 在宿主机文件缺失时会自动造同名目录挂进来:
    # 必须与"文件缺失"同义降级 vendor-only,而非 IsADirectoryError 打成启动 crash-loop
    from sentinel.app import _load_targets_or_disable

    mount_dir = tmp_path / "services.yaml"
    mount_dir.mkdir()
    assert _load_targets_or_disable(str(mount_dir)) == []


async def test_middleware_unconfigured_keeps_open_event(tmp_path, monkeypatch):
    # 曾配置 SENTINEL_MIDDLEWARE_METRICS 产生 open 事件,之后清空配置重启:
    # middleware 检查被跳过,scope 必须剔除 middleware_down——不评估≠恢复,
    # 不能给仍可能挂着的中间件发"已恢复"假绿卡
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("SENTINEL_SERVICES_FILE", str(tmp_path / "no-such.yaml"))
    monkeypatch.setenv("SENTINEL_PROMETHEUS_URL", "http://prometheus:9090")
    monkeypatch.setenv("SENTINEL_LOKI_URL", "")

    from sentinel.app import build_jobs
    from sentinel.config import Settings
    from sentinel.store import Store

    client = httpx.AsyncClient()
    store = Store(str(tmp_path / "s.db"))

    def pg_down_handler(request):
        q = request.url.params["query"]
        if "resets" in q or "oom" in q:
            return httpx.Response(200, json=_vector([]))
        if "pg_up" in q:
            return httpx.Response(200, json=_vector([({"__name__": "pg_up"}, 0)]))
        return httpx.Response(200, json=_vector([({}, 0.3)]))

    monkeypatch.setenv("SENTINEL_MIDDLEWARE_METRICS", "pg_up:postgres")
    jobs = {n: t for n, _, t in build_jobs(Settings(_env_file=None), client, store)}
    with respx.mock:
        respx.get("http://prometheus:9090/api/v1/query").mock(side_effect=pg_down_handler)
        respx.post("https://open.feishu.cn/hook/T").mock(
            return_value=httpx.Response(200, json={"code": 0})
        )
        await jobs["metrics_scan"]()
    assert [(e.rule, e.subject) for e in store.get_open_events()] == [
        ("middleware_down", "postgres")
    ]

    # 清空 CSV 重建闭包(模拟改 .env 重启),prom 全健康:事件必须保持 open,零发卡
    monkeypatch.setenv("SENTINEL_MIDDLEWARE_METRICS", "")
    jobs = {n: t for n, _, t in build_jobs(Settings(_env_file=None), client, store)}
    with respx.mock:
        respx.get("http://prometheus:9090/api/v1/query").mock(side_effect=_healthy_prom_handler)
        webhook = respx.post("https://open.feishu.cn/hook/T").mock(
            return_value=httpx.Response(200, json={"code": 0})
        )
        await jobs["metrics_scan"]()
    assert webhook.call_count == 0  # 没有假恢复卡
    assert [(e.rule, e.subject) for e in store.get_open_events()] == [
        ("middleware_down", "postgres")
    ]
    await client.aclose()
    store.close()


async def test_build_jobs_diagnosis_job_when_llm_configured(tmp_path, monkeypatch):
    # LLM 直连层条件装配:配了 base_url+model 才有 diagnosis job
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("SENTINEL_SERVICES_FILE", str(tmp_path / "no-such.yaml"))
    monkeypatch.setenv("SENTINEL_PROMETHEUS_URL", "")
    monkeypatch.setenv("SENTINEL_LOKI_URL", "")
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test/v1")
    monkeypatch.setenv("LLM_MODEL", "test-model")

    from sentinel.app import build_jobs
    from sentinel.config import Settings
    from sentinel.store import Store

    client = httpx.AsyncClient()
    store = Store(str(tmp_path / "s.db"))
    jobs = build_jobs(Settings(_env_file=None), client, store)
    assert [name for name, _, _ in jobs] == ["statuspage", "daily_report", "diagnosis"]
    await client.aclose()
    store.close()


async def test_diag_tick_done_and_failed_paths(tmp_path, monkeypatch):
    # 端到端:pending 事件被容器内 driver 诊断——成功路径落 done+发卡,
    # 解析不出 json 的路径两次尝试后落 failed 且不发卡
    import json

    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("SENTINEL_SERVICES_FILE", str(tmp_path / "no-such.yaml"))
    monkeypatch.setenv("SENTINEL_PROMETHEUS_URL", "")
    monkeypatch.setenv("SENTINEL_LOKI_URL", "")
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test/v1")
    monkeypatch.setenv("LLM_MODEL", "test-model")

    from sentinel.app import build_jobs
    from sentinel.config import Settings
    from sentinel.store import Store

    client = httpx.AsyncClient()
    store = Store(str(tmp_path / "s.db"))
    eid = store.insert_event(
        ts=1700000000,
        rule="mem_pressure",
        subject="api",
        severity="warning",
        status="open",
        detail="容器 api 内存高",
        payload_json="{}",
        diagnosis_status="pending",
        cooldown_until=0,
    )
    jobs = {n: t for n, _, t in build_jobs(Settings(_env_file=None), client, store)}

    diag = {"summary": "内存高", "root_cause": "泄漏", "confidence": "low"}
    final = "```json\n" + json.dumps(diag, ensure_ascii=False) + "\n```"
    with respx.mock:
        respx.post("http://llm.test/v1/chat/completions").mock(
            return_value=httpx.Response(
                200, json={"choices": [{"message": {"role": "assistant", "content": final}}]}
            )
        )
        webhook = respx.post("https://open.feishu.cn/hook/T").mock(
            return_value=httpx.Response(200, json={"code": 0})
        )
        await jobs["diagnosis"]()
    event = store.get_event(eid)
    assert event.diagnosis_status == "done"
    assert json.loads(event.diagnosis_json)["root_cause"] == "泄漏"
    assert webhook.call_count == 1
    body = json.loads(webhook.calls.last.request.content)
    assert "诊断" in body["card"]["header"]["title"]["content"]

    # failed 路径:模型不给 json → 两次尝试 → failed,原文留底,不发卡
    eid2 = store.insert_event(
        ts=1700000100,
        rule="mem_pressure",
        subject="db",
        severity="warning",
        status="open",
        detail="d",
        payload_json="{}",
        diagnosis_status="pending",
        cooldown_until=0,
    )
    with respx.mock:
        llm = respx.post("http://llm.test/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": "说不清"}}]},
            )
        )
        webhook = respx.post("https://open.feishu.cn/hook/T").mock(
            return_value=httpx.Response(200, json={"code": 0})
        )
        await jobs["diagnosis"]()
    event2 = store.get_event(eid2)
    assert event2.diagnosis_status == "failed"
    assert "说不清" in json.loads(event2.diagnosis_json)["raw"]
    assert llm.call_count == 2  # 两次尝试
    assert webhook.call_count == 0
    await client.aclose()
    store.close()


async def test_build_jobs_warns_middleware_without_prometheus(tmp_path, monkeypatch, caplog):
    # 半配置矛盾:配了中间件指标却关了 prometheus,兜底覆盖静默丢失——至少要响一声
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("SENTINEL_SERVICES_FILE", str(tmp_path / "no-such.yaml"))
    monkeypatch.setenv("SENTINEL_PROMETHEUS_URL", "")
    monkeypatch.setenv("SENTINEL_LOKI_URL", "")
    monkeypatch.setenv("SENTINEL_MIDDLEWARE_METRICS", "pg_up:postgres")

    from sentinel.app import build_jobs
    from sentinel.config import Settings
    from sentinel.store import Store

    client = httpx.AsyncClient()
    store = Store(str(tmp_path / "s.db"))
    with caplog.at_level("WARNING", logger="sentinel"):
        build_jobs(Settings(_env_file=None), client, store)
    assert any("SENTINEL_MIDDLEWARE_METRICS" in r.message for r in caplog.records)
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
    monkeypatch.setenv("SENTINEL_LOKI_URL", "http://loki:3100")

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
    monkeypatch.setenv("SENTINEL_PROMETHEUS_URL", "http://prometheus:9090")

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


async def test_build_jobs_warns_llm_half_config(tmp_path, monkeypatch, caplog):
    # 只填 LLM_BASE_URL 不填 LLM_MODEL(或反过来)= 静默不启用,至少要响一声
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("SENTINEL_SERVICES_FILE", str(tmp_path / "no-such.yaml"))
    monkeypatch.setenv("SENTINEL_PROMETHEUS_URL", "")
    monkeypatch.setenv("SENTINEL_LOKI_URL", "")
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test/v1")

    from sentinel.app import build_jobs
    from sentinel.config import Settings
    from sentinel.store import Store

    client = httpx.AsyncClient()
    store = Store(str(tmp_path / "s.db"))
    with caplog.at_level("WARNING", logger="sentinel"):
        jobs = build_jobs(Settings(_env_file=None), client, store)
    assert any("LLM_MODEL" in r.message for r in caplog.records)
    assert "diagnosis" not in [n for n, _, _ in jobs]
    await client.aclose()
    store.close()


class _FakeDocker:
    """build_jobs 的 docker 注入桩:只要 truthy 即触发 docker_scan 装配。
    docker_tick 经 monkeypatch 的 run_docker_scan 取数据,ps/aclose 兜空实现。"""

    async def ps(self, *, all=True) -> list:
        return []

    async def aclose(self) -> None:
        return None


async def test_build_jobs_appends_docker_scan_when_docker_present(tmp_path, monkeypatch):
    # docker=<fake> + SENTINEL_DOCKER_HOST 配置 → jobs 多出 ("docker_scan", 60, tick)
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("SENTINEL_SERVICES_FILE", str(tmp_path / "no-such.yaml"))
    monkeypatch.setenv("SENTINEL_PROMETHEUS_URL", "")
    monkeypatch.setenv("SENTINEL_LOKI_URL", "")
    monkeypatch.setenv("SENTINEL_DOCKER_HOST", "tcp://docker-proxy:2375")

    from sentinel.app import build_jobs
    from sentinel.config import Settings
    from sentinel.store import Store

    client = httpx.AsyncClient()
    store = Store(str(tmp_path / "s.db"))
    jobs = build_jobs(Settings(_env_file=None), client, store, docker=_FakeDocker())
    by_name = {name: interval for name, interval, _ in jobs}
    assert "docker_scan" in by_name
    assert by_name["docker_scan"] == 60  # sentinel_docker_scan_interval 默认值
    await client.aclose()
    store.close()


async def test_build_jobs_no_docker_scan_when_docker_none(tmp_path, monkeypatch):
    # docker=None(含旧三参调用)→ 不装 docker_scan,既有 job 列表不受影响
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("SENTINEL_SERVICES_FILE", str(tmp_path / "no-such.yaml"))
    monkeypatch.setenv("SENTINEL_PROMETHEUS_URL", "")
    monkeypatch.setenv("SENTINEL_LOKI_URL", "")

    from sentinel.app import build_jobs
    from sentinel.config import Settings
    from sentinel.store import Store

    client = httpx.AsyncClient()
    store = Store(str(tmp_path / "s.db"))
    settings = Settings(_env_file=None)
    assert [n for n, _, _ in build_jobs(settings, client, store, docker=None)] == [
        "statuspage",
        "daily_report",
    ]
    # 旧三参调用(无 docker)同样不装 docker_scan(docker 默认 None)
    assert "docker_scan" not in [n for n, _, _ in build_jobs(settings, client, store)]
    await client.aclose()
    store.close()


async def test_docker_tick_holds_then_recovers(tmp_path, monkeypatch):
    # 假绿卡纪律:open 的 container_down(web)在某轮 active 不含 web 时被 hold,
    # 不发恢复卡、事件保持 open;待 web 重新进 active 且无 finding,才发恢复卡并 resolved
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("SENTINEL_SERVICES_FILE", str(tmp_path / "no-such.yaml"))
    monkeypatch.setenv("SENTINEL_PROMETHEUS_URL", "")
    monkeypatch.setenv("SENTINEL_LOKI_URL", "")
    monkeypatch.setenv("SENTINEL_DOCKER_HOST", "tcp://docker-proxy:2375")

    import sentinel.app as app_mod
    from sentinel.config import Settings
    from sentinel.store import Store

    client = httpx.AsyncClient()
    store = Store(str(tmp_path / "s.db"))
    store.insert_event(
        ts=1000,
        rule="container_down",
        subject="web",
        severity="critical",
        status="open",
        detail="容器 web 停止",
        payload_json="{}",
        diagnosis_status="pending",
        cooldown_until=22600,
    )
    jobs = {
        n: t
        for n, _, t in app_mod.build_jobs(
            Settings(_env_file=None), client, store, docker=_FakeDocker()
        )
    }

    # 第 1 轮:扫描无 finding,但 active 不含 web(web 消失/被过滤)→ hold,不发恢复卡
    async def scan_held(docker, settings, *, now_ts, emit_oom):
        return [], {"db", "cache"}

    monkeypatch.setattr(app_mod, "run_docker_scan", scan_held)
    with respx.mock:
        webhook = respx.post("https://open.feishu.cn/hook/T").mock(
            return_value=httpx.Response(200, json={"code": 0})
        )
        await jobs["docker_scan"]()
    assert webhook.call_count == 0  # 被 hold,无假绿卡
    assert [(e.rule, e.subject) for e in store.get_open_events()] == [("container_down", "web")]

    # 第 2 轮:active 含 web 且无 finding(容器恢复 running)→ 发恢复卡 + resolved
    async def scan_recovered(docker, settings, *, now_ts, emit_oom):
        return [], {"web", "db"}

    monkeypatch.setattr(app_mod, "run_docker_scan", scan_recovered)
    with respx.mock:
        webhook = respx.post("https://open.feishu.cn/hook/T").mock(
            return_value=httpx.Response(200, json={"code": 0})
        )
        await jobs["docker_scan"]()
    assert webhook.call_count == 1
    import json as _json

    body = _json.loads(webhook.calls.last.request.content)
    assert "已恢复" in body["card"]["header"]["title"]["content"]
    assert store.get_open_events() == []  # web 已 resolved
    await client.aclose()
    store.close()


async def test_docker_scan_consecutive_failures_escalate_and_scope_discipline(
    tmp_path, monkeypatch
):  # noqa: E501
    # docker.ps 连续失败 3 次 → scan_failed_docker 升级发卡;
    # 期间另有 open 的 container_down 事件,失败轮 scope 不含 DOCKER_RULES → 不被假恢复
    import json

    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("SENTINEL_SERVICES_FILE", str(tmp_path / "no-such.yaml"))
    monkeypatch.setenv("SENTINEL_PROMETHEUS_URL", "")
    monkeypatch.setenv("SENTINEL_LOKI_URL", "")
    monkeypatch.setenv("SENTINEL_DOCKER_HOST", "tcp://docker-proxy:2375")

    import sentinel.app as app_mod
    from sentinel.config import Settings
    from sentinel.store import Store

    client = httpx.AsyncClient()
    store = Store(str(tmp_path / "s.db"))
    store.insert_event(
        ts=1000,
        rule="container_down",
        subject="api",
        severity="critical",
        status="open",
        detail="容器 api 停止",
        payload_json="{}",
        diagnosis_status="pending",
        cooldown_until=22600,
    )
    jobs = {
        n: t
        for n, _, t in app_mod.build_jobs(
            Settings(_env_file=None), client, store, docker=_FakeDocker()
        )
    }

    async def scan_boom(docker, settings, *, now_ts, emit_oom):
        raise RuntimeError("docker socket unreachable")

    monkeypatch.setattr(app_mod, "run_docker_scan", scan_boom)
    with respx.mock:
        webhook = respx.post("https://open.feishu.cn/hook/T").mock(
            return_value=httpx.Response(200, json={"code": 0})
        )
        for _ in range(3):
            await jobs["docker_scan"]()  # tick 自吞 ps 异常,连续 3 次后升级发卡
    assert webhook.call_count == 1  # 仅 scan_failed_docker 一张,container_down 未被假恢复
    body = json.loads(webhook.calls.last.request.content)
    assert "Docker 巡检失败" in body["card"]["header"]["title"]["content"]
    opens = {(e.rule, e.subject) for e in store.get_open_events()}
    assert opens == {("container_down", "api"), ("scan_failed_docker", "docker_scan")}
    await client.aclose()
    store.close()


async def test_docker_tick_emit_oom_follows_prom_disabled(tmp_path, monkeypatch):
    # _prom_enabled 决定 emit_oom:prom URL 已配 → _prom_enabled True → emit_oom=False
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("SENTINEL_SERVICES_FILE", str(tmp_path / "no-such.yaml"))
    monkeypatch.setenv("SENTINEL_PROMETHEUS_URL", "http://prometheus:9090")
    monkeypatch.setenv("SENTINEL_LOKI_URL", "")
    monkeypatch.setenv("SENTINEL_DOCKER_HOST", "tcp://docker-proxy:2375")

    import sentinel.app as app_mod
    from sentinel.config import Settings
    from sentinel.store import Store

    client = httpx.AsyncClient()
    store = Store(str(tmp_path / "s.db"))
    jobs = {
        n: t
        for n, _, t in app_mod.build_jobs(
            Settings(_env_file=None), client, store, docker=_FakeDocker()
        )
    }

    seen = {}

    async def scan_capture(docker, settings, *, now_ts, emit_oom):
        seen["emit_oom"] = emit_oom
        return [], set()

    monkeypatch.setattr(app_mod, "run_docker_scan", scan_capture)
    with respx.mock:
        respx.post("https://open.feishu.cn/hook/T").mock(
            return_value=httpx.Response(200, json={"code": 0})
        )
        await jobs["docker_scan"]()
    assert seen["emit_oom"] is False  # prom 开 → docker 不重复发 OOM(交由 prom)
    await client.aclose()
    store.close()


async def test_lifespan_constructs_and_closes_docker_client(tmp_path, monkeypatch):
    # 唯一进入 lifespan 的测试:验证 docker_endpoint 非空时构造 DockerClient、
    # 且 finally 中 await aclose()(否则 UDS transport fd 泄漏)。
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("SENTINEL_SERVICES_FILE", str(tmp_path / "no-such.yaml"))
    monkeypatch.setenv("SENTINEL_DOCKER_HOST", "tcp://docker-proxy:2375")

    import sentinel.app as app_mod

    constructed: list[str] = []
    closed: list[bool] = []

    class _RecordingDocker:
        def __init__(self, endpoint, **_kw):
            constructed.append(endpoint)

        async def ps(self, *, all=True):
            return []

        async def aclose(self):
            closed.append(True)

    monkeypatch.setattr(app_mod, "DockerClient", _RecordingDocker)
    monkeypatch.setattr(app_mod, "build_jobs", lambda *a, **k: [])  # 不起任何 job 循环

    app = app_mod.FastAPI()
    async with app_mod.lifespan(app):
        pass

    assert constructed == ["tcp://docker-proxy:2375"]  # 用 docker_endpoint 构造
    assert closed == [True]  # finally 中被 aclose
