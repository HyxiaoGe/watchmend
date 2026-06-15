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
    monkeypatch.setenv("SENTINEL_LLM_CONFIG_FILE", str(tmp_path / "no-llm.yaml"))

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


async def test_build_jobs_no_half_config_warning_when_yaml_active(tmp_path, monkeypatch, caplog):
    # 有效 llm.yaml 在管 → env LLM_* 半配置无关紧要,不该误报半配警告;且 diagnosis 正常装配
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("SENTINEL_SERVICES_FILE", str(tmp_path / "no-such.yaml"))
    monkeypatch.setenv("SENTINEL_PROMETHEUS_URL", "")
    monkeypatch.setenv("SENTINEL_LOKI_URL", "")
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test/v1")  # 半配:只有 base_url,无 model
    yaml_path = tmp_path / "llm.yaml"
    yaml_path.write_text(
        "active: a\nproviders:\n  a:\n    base_url: http://x/v1\n    model: m\n    api_key: ''\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SENTINEL_LLM_CONFIG_FILE", str(yaml_path))

    from sentinel.app import build_jobs
    from sentinel.config import Settings
    from sentinel.store import Store

    client = httpx.AsyncClient()
    store = Store(str(tmp_path / "s.db"))
    with caplog.at_level("WARNING", logger="sentinel"):
        jobs = build_jobs(Settings(_env_file=None), client, store)
    assert not any("LLM_MODEL" in r.message for r in caplog.records)  # llm.yaml 在管,不误报
    assert "diagnosis" in [n for n, _, _ in jobs]
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
        return [], {"db", "cache"}, {}

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
        return [], {"web", "db"}, {}

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
        return [], set(), {}

    monkeypatch.setattr(app_mod, "run_docker_scan", scan_capture)
    with respx.mock:
        respx.post("https://open.feishu.cn/hook/T").mock(
            return_value=httpx.Response(200, json={"code": 0})
        )
        await jobs["docker_scan"]()
    assert seen["emit_oom"] is False  # prom 开 + metrics 健康 → docker 不重复发 OOM(交由 prom)
    await client.aclose()
    store.close()


async def test_docker_tick_emit_oom_failopen_when_metrics_unhealthy(tmp_path, monkeypatch):
    # fail-open:prom 已配但 metrics 巡检在失败(cAdvisor/prom 挂)→ docker 接管 OOM,
    # 不因 prom URL 在场就静默丢弃 OOMKilled(宁可重不可漏)
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

    async def metrics_boom(prom, settings):
        raise RuntimeError("prometheus unreachable")

    monkeypatch.setattr(app_mod, "run_metrics_scan", metrics_boom)

    seen = {}

    async def scan_capture(docker, settings, *, now_ts, emit_oom):
        seen["emit_oom"] = emit_oom
        return [], set(), {}

    monkeypatch.setattr(app_mod, "run_docker_scan", scan_capture)
    with respx.mock:
        respx.post("https://open.feishu.cn/hook/T").mock(
            return_value=httpx.Response(200, json={"code": 0})
        )
        await jobs["metrics_scan"]()  # 一次失败 → scan_fails["metrics"] = 1(未达阈值,不发卡)
        await jobs["docker_scan"]()
    assert seen["emit_oom"] is True  # metrics 不健康 → docker 接管 OOM
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


async def test_lifespan_sets_diag_job_registered(tmp_path, monkeypatch):
    # lifespan 据 build_jobs 结果落 app.state.diag_job_registered,供面板区分"在跑/待重启"
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("SENTINEL_SERVICES_FILE", str(tmp_path / "no-such.yaml"))

    import sentinel.app as app_mod

    async def _noop_tick() -> None:
        return None

    monkeypatch.setattr(app_mod, "build_jobs", lambda *a, **k: [("diagnosis", 3600.0, _noop_tick)])
    app = app_mod.FastAPI()
    async with app_mod.lifespan(app):
        assert app.state.diag_job_registered is True

    monkeypatch.setattr(app_mod, "build_jobs", lambda *a, **k: [("statuspage", 3600.0, _noop_tick)])
    app2 = app_mod.FastAPI()
    async with app_mod.lifespan(app2):
        assert app2.state.diag_job_registered is False


async def test_build_jobs_requires_at_least_one_channel(tmp_path, monkeypatch):
    # 零渠道:不配飞书也不配任何新渠道 → build_jobs 响亮失败
    import pytest

    monkeypatch.delenv("FEISHU_VENDOR_WEBHOOK", raising=False)
    monkeypatch.delenv("FEISHU_PATROL_WEBHOOK", raising=False)
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("SENTINEL_SERVICES_FILE", str(tmp_path / "no-such.yaml"))
    monkeypatch.setenv("SENTINEL_PROMETHEUS_URL", "")
    monkeypatch.setenv("SENTINEL_LOKI_URL", "")

    from sentinel.app import build_jobs
    from sentinel.config import Settings
    from sentinel.store import Store

    client = httpx.AsyncClient()
    store = Store(str(tmp_path / "s.db"))
    with pytest.raises(ValueError, match="至少配置一个通知渠道"):
        build_jobs(Settings(_env_file=None), client, store)
    await client.aclose()
    store.close()


async def test_build_jobs_telegram_only_starts(tmp_path, monkeypatch):
    # 海外用户只配 Telegram(无飞书)也能起来:statuspage + daily_report
    monkeypatch.delenv("FEISHU_VENDOR_WEBHOOK", raising=False)
    monkeypatch.delenv("FEISHU_PATROL_WEBHOOK", raising=False)
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("SENTINEL_SERVICES_FILE", str(tmp_path / "no-such.yaml"))
    monkeypatch.setenv("SENTINEL_PROMETHEUS_URL", "")
    monkeypatch.setenv("SENTINEL_LOKI_URL", "")
    monkeypatch.setenv("SENTINEL_TELEGRAM_BOT_TOKEN", "TESTtok123")
    monkeypatch.setenv("SENTINEL_TELEGRAM_CHAT_ID", "-100200300")

    from sentinel.app import build_jobs
    from sentinel.config import Settings
    from sentinel.store import Store

    client = httpx.AsyncClient()
    store = Store(str(tmp_path / "s.db"))
    jobs = build_jobs(Settings(_env_file=None), client, store)
    assert [n for n, _, _ in jobs] == ["statuspage", "daily_report"]
    await client.aclose()
    store.close()


async def test_patrol_only_feishu_still_delivers_vendor_stream(tmp_path, monkeypatch):
    # 只配 FEISHU_PATROL_WEBHOOK(vendor 留空):vendor 流(状态页/心跳)必须回退到 patrol 群,
    # 否则 vendor broadcaster 为空、心跳与状态页事件静默不发(Codex P2)。
    monkeypatch.delenv("FEISHU_VENDOR_WEBHOOK", raising=False)
    monkeypatch.setenv("FEISHU_PATROL_WEBHOOK", "https://open.feishu.cn/hook/PATROL")
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("SENTINEL_SERVICES_FILE", str(tmp_path / "no-such.yaml"))
    monkeypatch.setenv("SENTINEL_PROMETHEUS_URL", "")
    monkeypatch.setenv("SENTINEL_LOKI_URL", "")
    monkeypatch.setenv(
        "SENTINEL_PROVIDERS", ""
    )  # 无外部状态页 → run_cycle 空转,单测心跳走 vendor 流
    monkeypatch.setenv("SENTINEL_HEARTBEAT_ENABLED", "true")
    monkeypatch.setenv("SENTINEL_HEARTBEAT_HOUR", "0")  # 0 点已过 → 启动即补发一张心跳

    from sentinel.app import build_jobs
    from sentinel.config import Settings
    from sentinel.store import Store

    settings = Settings(_env_file=None)
    client = httpx.AsyncClient()
    store = Store(settings.sentinel_db_path)
    jobs = {n: t for n, _, t in build_jobs(settings, client, store)}

    with respx.mock:
        patrol = respx.post("https://open.feishu.cn/hook/PATROL").mock(
            return_value=httpx.Response(200, json={"code": 0})
        )
        await jobs["statuspage"]()  # run_cycle(空) + run_heartbeat → vendor_broadcaster
    assert patrol.call_count == 1  # 心跳经 vendor 流投递到 patrol 群,回退生效
    await client.aclose()
    store.close()


async def test_alert_fans_out_to_feishu_and_telegram(tmp_path, monkeypatch):
    # 同时配飞书 + Telegram:metrics 连续失败升级的告警必须同时投递到两个端点(广播)
    import json

    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("SENTINEL_SERVICES_FILE", str(tmp_path / "no-such.yaml"))
    monkeypatch.setenv("SENTINEL_PROMETHEUS_URL", "http://prometheus:9090")
    monkeypatch.setenv("SENTINEL_LOKI_URL", "")
    monkeypatch.setenv("SENTINEL_TELEGRAM_BOT_TOKEN", "TESTtok123")
    monkeypatch.setenv("SENTINEL_TELEGRAM_CHAT_ID", "-100200300")

    from sentinel.app import build_jobs
    from sentinel.config import Settings
    from sentinel.store import Store

    settings = Settings(_env_file=None)
    client = httpx.AsyncClient()
    store = Store(settings.sentinel_db_path)
    jobs = {n: t for n, _, t in build_jobs(settings, client, store)}

    with respx.mock:
        respx.get("http://prometheus:9090/api/v1/query").mock(return_value=httpx.Response(500))
        feishu = respx.post("https://open.feishu.cn/hook/T").mock(
            return_value=httpx.Response(200, json={"code": 0})
        )
        tg = respx.post("https://api.telegram.org/botTESTtok123/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        for _ in range(3):
            await jobs["metrics_scan"]()  # 连续 3 次失败 → 升级 scan_failed_prometheus 告警
    assert feishu.call_count == 1
    assert tg.call_count == 1  # 同一告警 fan-out 到 Telegram
    body = json.loads(tg.calls.last.request.content)
    assert "Prometheus 巡检失败" in body["text"]
    assert body["text"].startswith("🟠")  # warning 严重度前导 emoji
    await client.aclose()
    store.close()


async def test_diag_tick_persists_tool_calls_on_done_and_failed(tmp_path, monkeypatch):
    import json

    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("SENTINEL_SERVICES_FILE", str(tmp_path / "no-such.yaml"))
    monkeypatch.setenv("SENTINEL_PROMETHEUS_URL", "http://prometheus:9090")
    monkeypatch.setenv("SENTINEL_LOKI_URL", "")
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test/v1")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("SENTINEL_LLM_CONFIG_FILE", str(tmp_path / "no-llm.yaml"))

    from sentinel.app import build_jobs
    from sentinel.config import Settings
    from sentinel.store import Store

    client = httpx.AsyncClient()
    store = Store(str(tmp_path / "s.db"))
    eid = store.insert_event(
        ts=1700000000,
        rule="container_down",
        subject="pg",
        severity="critical",
        status="open",
        detail="d",
        payload_json="{}",
        diagnosis_status="pending",
        cooldown_until=0,
    )
    jobs = {n: t for n, _, t in build_jobs(Settings(_env_file=None), client, store)}

    diag = {"summary": "OOM", "root_cause": "内存不足", "confidence": "high"}
    final = "```json\n" + json.dumps(diag, ensure_ascii=False) + "\n```"
    tool_call = [
        {
            "id": "c1",
            "type": "function",
            "function": {"name": "prom_query", "arguments": '{"query": "up"}'},
        }
    ]
    with respx.mock:
        respx.post("http://llm.test/v1/chat/completions").mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": tool_call,
                                }
                            }
                        ]
                    },
                ),
                httpx.Response(
                    200,
                    json={"choices": [{"message": {"role": "assistant", "content": final}}]},
                ),
            ]
        )
        respx.get("http://prometheus:9090/api/v1/query").mock(
            return_value=httpx.Response(200, json={"status": "success", "data": {}})
        )
        respx.post("https://open.feishu.cn/hook/T").mock(
            return_value=httpx.Response(200, json={"code": 0})
        )
        await jobs["diagnosis"]()
    e = store.get_event(eid)
    assert e.diagnosis_status == "done"
    captured = json.loads(e.diagnosis_tools_json)
    assert captured[0]["tool"] == "prom_query"
    assert captured[0]["ok"] is True

    # failed 路径:模型每轮给工具但最终给不出可解析 json → 两次尝试后 failed,证据链仍落库
    eid2 = store.insert_event(
        ts=1700000100,
        rule="container_down",
        subject="db",
        severity="critical",
        status="open",
        detail="d",
        payload_json="{}",
        diagnosis_status="pending",
        cooldown_until=0,
    )
    with respx.mock:
        respx.post("http://llm.test/v1/chat/completions").mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": tool_call,
                                }
                            }
                        ]
                    },
                ),
                httpx.Response(
                    200, json={"choices": [{"message": {"role": "assistant", "content": "说不清"}}]}
                ),
                httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": tool_call,
                                }
                            }
                        ]
                    },
                ),
                httpx.Response(
                    200,
                    json={"choices": [{"message": {"role": "assistant", "content": "还是说不清"}}]},
                ),
            ]
        )
        respx.get("http://prometheus:9090/api/v1/query").mock(
            return_value=httpx.Response(200, json={"status": "success", "data": {}})
        )
        await jobs["diagnosis"]()
    e2 = store.get_event(eid2)
    assert e2.diagnosis_status == "failed"
    assert json.loads(e2.diagnosis_tools_json)[0]["tool"] == "prom_query"
    await client.aclose()
    store.close()


async def test_post_diagnosis_accepts_optional_tools(tmp_path, monkeypatch):
    import json

    from fastapi import FastAPI

    from sentinel.api import register_routes
    from sentinel.config import Settings
    from sentinel.notify.base import Broadcaster
    from sentinel.store import Store

    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    store = Store(str(tmp_path / "s.db"))
    eid = store.insert_event(
        ts=1700000000,
        rule="container_down",
        subject="pg",
        severity="critical",
        status="open",
        detail="d",
        payload_json="{}",
        diagnosis_status="pending",
        cooldown_until=0,
    )
    app = FastAPI()
    register_routes(app)
    app.state.store = store
    app.state.settings = Settings(_env_file=None)
    app.state.patrol_broadcaster = Broadcaster([])  # 无渠道,send→0,不发卡不出网
    app.state.services = []

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.post(
            f"/events/{eid}/diagnosis",
            json={
                "status": "done",
                "diagnosis": {"summary": "s", "root_cause": "r"},
                "tools": [{"tool": "docker_ps", "args": {}, "output": "x", "ok": True}],
            },
        )
    assert resp.status_code == 200
    e = store.get_event(eid)
    assert json.loads(e.diagnosis_tools_json)[0]["tool"] == "docker_ps"
    store.close()


def _diag_app(store, monkeypatch):
    from fastapi import FastAPI

    from sentinel.api import register_routes
    from sentinel.config import Settings
    from sentinel.notify.base import Broadcaster

    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    app = FastAPI()
    register_routes(app)
    app.state.store = store
    app.state.settings = Settings(_env_file=None)
    app.state.patrol_broadcaster = Broadcaster([])  # 无渠道,send→0,不发卡不出网
    app.state.services = []
    return app


def _pending_event(store):
    return store.insert_event(
        ts=1700000000,
        rule="container_down",
        subject="pg",
        severity="critical",
        status="open",
        detail="d",
        payload_json="{}",
        diagnosis_status="pending",
        cooldown_until=0,
    )


async def test_post_diagnosis_caps_tool_output(tmp_path, monkeypatch):
    # 回填路径必须与容器内 _tool_loop 同口径:每项 output 截断到 PANEL_TOOL_OUTPUT_CAP
    import json

    from sentinel.llm_driver import PANEL_TOOL_OUTPUT_CAP
    from sentinel.store import Store

    store = Store(str(tmp_path / "s.db"))
    eid = _pending_event(store)
    app = _diag_app(store, monkeypatch)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.post(
            f"/events/{eid}/diagnosis",
            json={
                "status": "done",
                "diagnosis": {"summary": "s", "root_cause": "r"},
                "tools": [{"tool": "docker_logs", "args": {}, "output": "x" * 6000, "ok": True}],
            },
        )
    assert resp.status_code == 200
    stored = json.loads(store.get_event(eid).diagnosis_tools_json)[0]["output"]
    assert stored.endswith("…(truncated)")
    assert len(stored) == PANEL_TOOL_OUTPUT_CAP + len("…(truncated)")
    store.close()


async def test_post_diagnosis_omitted_tools_preserves_evidence(tmp_path, monkeypatch):
    # status-only 回填(无 tools 字段)绝不能抹掉既有证据链
    import json

    from sentinel.store import Store

    store = Store(str(tmp_path / "s.db"))
    eid = _pending_event(store)
    seeded = [{"tool": "prom_query", "args": {"query": "up"}, "output": "1", "ok": True}]
    store.set_diagnosis(
        eid,
        status="done",
        diagnosis_json=json.dumps({"summary": "s"}),
        tools_json=json.dumps(seeded),
    )
    app = _diag_app(store, monkeypatch)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.post(
            f"/events/{eid}/diagnosis",
            json={"status": "done", "diagnosis": {"summary": "s2"}},
        )
    assert resp.status_code == 200
    preserved = store.get_event(eid).diagnosis_tools_json
    assert preserved is not None
    assert json.loads(preserved)[0]["tool"] == "prom_query"
    store.close()


async def test_post_diagnosis_explicit_empty_tools_clears(tmp_path, monkeypatch):
    # 显式 tools=[] = 明确清空该列
    import json

    from sentinel.store import Store

    store = Store(str(tmp_path / "s.db"))
    eid = _pending_event(store)
    store.set_diagnosis(
        eid,
        status="done",
        diagnosis_json=json.dumps({"summary": "s"}),
        tools_json=json.dumps([{"tool": "prom_query", "args": {}, "output": "1", "ok": True}]),
    )
    app = _diag_app(store, monkeypatch)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.post(
            f"/events/{eid}/diagnosis",
            json={"status": "done", "diagnosis": {"summary": "s2"}, "tools": []},
        )
    assert resp.status_code == 200
    assert store.get_event(eid).diagnosis_tools_json is None
    store.close()


async def test_docker_tick_crashloop_gated_when_metrics_healthy(tmp_path, monkeypatch):
    # prom 在 + metrics 健康 → emit_crashloop False → detect_crashloops 不跑 → 不建基线
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

    async def scan_one(docker, settings, *, now_ts, emit_oom):
        return [], set(), {"x": 7}

    monkeypatch.setattr(app_mod, "run_docker_scan", scan_one)
    with respx.mock:
        respx.post("https://open.feishu.cn/hook/T").mock(
            return_value=httpx.Response(200, json={"code": 0})
        )
        await jobs["docker_scan"]()
    assert store.get_restart_baseline("x") is None  # gated off → no baseline written
    await client.aclose()
    store.close()


async def test_docker_tick_crashloop_runs_when_metrics_unhealthy(tmp_path, monkeypatch):
    # prom 在但 metrics 巡检失败 → emit_crashloop True → detect_crashloops 接管 → 建基线
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

    async def metrics_boom(prom, settings):
        raise RuntimeError("prometheus unreachable")

    async def scan_one(docker, settings, *, now_ts, emit_oom):
        return [], set(), {"x": 7}

    monkeypatch.setattr(app_mod, "run_metrics_scan", metrics_boom)
    monkeypatch.setattr(app_mod, "run_docker_scan", scan_one)
    with respx.mock:
        respx.post("https://open.feishu.cn/hook/T").mock(
            return_value=httpx.Response(200, json={"code": 0})
        )
        await jobs["metrics_scan"]()  # scan_fails["metrics"] = 1
        await jobs["docker_scan"]()
    baseline = store.get_restart_baseline("x")
    assert baseline is not None and baseline[0] == 7  # gated on → baseline seeded
    await client.aclose()
    store.close()


async def test_docker_tick_crashloop_fires_event_over_two_ticks(tmp_path, monkeypatch):
    # prom 缺席 → emit_crashloop True 恒成立。tick1 建基线(rc=1),tick2 rc=5(delta=4>=3)
    # → 落一条 container_crashloop 事件(point,resolved)。
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
    jobs = {
        n: t
        for n, _, t in app_mod.build_jobs(
            Settings(_env_file=None), client, store, docker=_FakeDocker()
        )
    }

    seq = iter([{"x": 1}, {"x": 5}])

    async def scan_seq(docker, settings, *, now_ts, emit_oom):
        return [], set(), next(seq)

    monkeypatch.setattr(app_mod, "run_docker_scan", scan_seq)
    with respx.mock:
        respx.post("https://open.feishu.cn/hook/T").mock(
            return_value=httpx.Response(200, json={"code": 0})
        )
        await jobs["docker_scan"]()  # tick1: seed baseline (1, now)
        await jobs["docker_scan"]()  # tick2: delta 4 >= 3 → fire
    events = [
        (e.rule, e.status, e.diagnosis_status)
        for e in store.get_events_since(0)
        if e.rule == "container_crashloop"
    ]
    assert events == [("container_crashloop", "resolved", "pending")]
    await client.aclose()
    store.close()
