# src/sentinel/app.py
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI

from sentinel.api import register_routes
from sentinel.config import Settings
from sentinel.engine import apply_findings
from sentinel.feishu.cards import build_diagnosis_card, build_summary_card
from sentinel.feishu.client import FeishuClient
from sentinel.fetcher import Fetcher
from sentinel.findings import LOG_RULES, METRICS_RULES, PROBE_RULES, Finding
from sentinel.llm_driver import LLMDriver
from sentinel.logql import LokiClient
from sentinel.poller import PollState, run_cycle, run_heartbeat
from sentinel.probe import load_targets, run_probe_cycle
from sentinel.probe_rules import evaluate_probe_rules
from sentinel.promql import PromClient
from sentinel.registry import build_adapters
from sentinel.report import build_daily_stats, report_due, run_daily_report
from sentinel.scan_hygiene import run_hygiene
from sentinel.scan_logs import run_log_scan
from sentinel.scan_metrics import run_metrics_scan
from sentinel.store import Store

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sentinel")

Tick = Callable[[], Awaitable[None]]

_REPORT_CHECK_INTERVAL = 60  # 日报门控轮询间隔(秒):到点才真正发
_DIAG_MAX_PER_TICK = 3  # 单轮最多诊断事件数,防事件风暴时 LLM 调用失控
_DIAG_ATTEMPTS = 2  # 同一事件最多尝试次数,之后落 failed(可经 API 人工重置)


async def _job_loop(name: str, interval: float, tick: Tick) -> None:
    """通用 Job 循环:单轮异常只记日志绝不退出;各 Job 独立任务互不影响。"""
    while True:
        try:
            await tick()
        except Exception:
            logger.exception("job %s tick crashed", name)
        await asyncio.sleep(interval)


def _local_tz(settings: Settings) -> timezone:
    return timezone(timedelta(hours=settings.sentinel_heartbeat_utc_offset))


def _load_targets_or_disable(path: str):
    """services 文件缺失 ≠ 致命错误:回退 vendor-only 模式(只监控外部状态页),
    内部探针层整体不启用。文件存在但格式坏仍然抛出(显式配置错误要响亮失败)。"""
    if Path(path).is_dir():
        # compose 短语法 bind mount 在宿主机文件缺失时会自动造一个同名目录挂进来,
        # 容器内看到的是空目录而非 ENOENT——与"文件缺失"同义,同样降级,
        # 不能放 IsADirectoryError 逃出去把启动打成 crash-loop
        logger.warning(
            "services path %s is a directory (host file missing?): "
            "internal probing disabled (vendor-only mode)",
            path,
        )
        return []
    try:
        return load_targets(path)
    except FileNotFoundError:
        logger.warning(
            "services file %s missing: internal probing disabled (vendor-only mode)", path
        )
        return []


def build_jobs(
    settings: Settings, client: httpx.AsyncClient, store: Store
) -> list[tuple[str, float, Tick]]:
    """组装全部 Job:(名称, 周期秒, tick)。lifespan 据此为每个 Job 起独立任务。"""
    adapters = build_adapters(settings.providers_list)
    fetcher = Fetcher(client)
    vendor_feishu = FeishuClient(
        client, settings.feishu_vendor_webhook, secret=settings.feishu_vendor_sign_secret
    )
    patrol_feishu = FeishuClient(
        client, settings.patrol_webhook, secret=settings.patrol_sign_secret
    )
    state = PollState()
    targets = _load_targets_or_disable(settings.sentinel_services_file)
    service_names = [t.name for t in targets]
    prom = PromClient(client, settings.sentinel_prometheus_url)
    loki = LokiClient(client, settings.sentinel_loki_url)
    driver = LLMDriver(client, settings)
    cooldown_seconds = settings.sentinel_cooldown_hours * 3600
    scan_fails = {"metrics": 0, "logs": 0}  # 数据源连续失败计数(进程内,重启清零)
    if settings.sentinel_middleware_metrics and not settings.sentinel_prometheus_url:
        logger.warning(
            "SENTINEL_MIDDLEWARE_METRICS 已配置但 SENTINEL_PROMETHEUS_URL 为空,"
            "中间件兜底检查不会运行"
        )
    logger.info(
        "jobs built: providers=%s probes=%d",
        [a.provider for a in adapters],
        len(targets),
    )

    def _now() -> tuple[int, str, datetime]:
        now_local = datetime.now(_local_tz(settings))
        return int(now_local.timestamp()), now_local.strftime("%Y-%m-%d %H:%M:%S"), now_local

    async def statuspage_tick() -> None:
        await run_cycle(
            adapters,
            fetcher=fetcher,
            store=store,
            feishu=vendor_feishu,
            state=state,
            verbosity=settings.sentinel_incident_verbosity,
            fail_threshold=settings.sentinel_fail_threshold,
        )
        if settings.sentinel_heartbeat_enabled:
            try:
                await run_heartbeat(
                    settings.providers_list,
                    store=store,
                    feishu=vendor_feishu,
                    now_local=datetime.now(_local_tz(settings)),
                    hour=settings.sentinel_heartbeat_hour,
                    interval=settings.sentinel_poll_interval,
                )
            except Exception:  # 心跳失败绝不影响主轮询
                logger.exception("heartbeat failed")

    async def probe_tick() -> None:
        now_ts, now_str, now_local = _now()
        await run_probe_cycle(targets, client=client, store=store, now_ts=now_ts)
        findings, hold = evaluate_probe_rules(
            store, service_names, settings, now_ts=now_ts, date_str=now_local.date().isoformat()
        )
        await apply_findings(
            findings,
            scope=PROBE_RULES,
            store=store,
            feishu=patrol_feishu,
            now_ts=now_ts,
            now_str=now_str,
            cooldown_seconds=cooldown_seconds,
            hold=hold,
        )

    async def metrics_tick() -> None:
        now_ts, now_str, _ = _now()
        # scope 只装"本轮状态确定"的规则。失败但计数未达阈值时 scan_failed_* 也不进:
        # 计数在进程内、部署即清零,若无条件进 scope,数据源仍挂时重启一次就会把
        # open 的巡检失败事件误判恢复(假绿卡),且重开还被 6h 冷却拦住。
        scope: set[str] = set()
        findings: list[Finding] = []
        try:
            findings = await run_metrics_scan(prom, settings)
            scan_fails["metrics"] = 0
            scope |= METRICS_RULES | {"scan_failed_prometheus"}
            if not settings.middleware_subjects:
                # CSV 空=middleware 检查整体被跳过:不评估≠恢复,
                # 不能让残留的 open middleware_down 事件被本轮假恢复
                scope.discard("middleware_down")
        except Exception:
            scan_fails["metrics"] += 1
            logger.exception("metrics scan failed (consecutive=%d)", scan_fails["metrics"])
        if scan_fails["metrics"] >= settings.sentinel_scan_fail_threshold:
            scope.add("scan_failed_prometheus")
            findings.append(
                Finding(
                    rule="scan_failed_prometheus",
                    subject="metrics_scan",
                    severity="warning",
                    detail=f"Prometheus 查询连续失败 {scan_fails['metrics']} 次,指标规则停摆",
                )
            )
        await apply_findings(
            findings,
            scope=scope,
            store=store,
            feishu=patrol_feishu,
            now_ts=now_ts,
            now_str=now_str,
            cooldown_seconds=cooldown_seconds,
        )

    async def logs_tick() -> None:
        now_ts, now_str, _ = _now()
        scope: set[str] = set()  # 同 metrics_tick 的 scope 纪律
        findings: list[Finding] = []
        try:
            findings = await run_log_scan(loki, settings, now_ts=now_ts)
            scan_fails["logs"] = 0
            scope |= LOG_RULES | {"scan_failed_loki"}
        except Exception:
            scan_fails["logs"] += 1
            logger.exception("log scan failed (consecutive=%d)", scan_fails["logs"])
        if scan_fails["logs"] >= settings.sentinel_scan_fail_threshold:
            scope.add("scan_failed_loki")
            findings.append(
                Finding(
                    rule="scan_failed_loki",
                    subject="log_scan",
                    severity="warning",
                    detail=f"Loki 查询连续失败 {scan_fails['logs']} 次,日志规则停摆",
                )
            )
        await apply_findings(
            findings,
            scope=scope,
            store=store,
            feishu=patrol_feishu,
            now_ts=now_ts,
            now_str=now_str,
            cooldown_seconds=cooldown_seconds,
        )

    async def diag_tick() -> None:
        # 容器内直连诊断:与宿主机 openclaw 编排(host/ 三件套)二选一,
        # 同抢 pending 队列,两条路径都开会重复诊断
        for event in store.get_pending_diagnosis_events()[:_DIAG_MAX_PER_TICK]:
            _, now_str, _ = _now()
            diagnosis, raw = None, ""
            for attempt in range(1, _DIAG_ATTEMPTS + 1):
                try:
                    diagnosis, raw = await driver.diagnose(event)
                except Exception as exc:
                    logger.exception("diagnose event %d attempt %d", event.id, attempt)
                    raw = f"llm error: {exc}"
                    continue
                if diagnosis:
                    break
            if diagnosis:
                store.set_diagnosis(
                    event.id,
                    status="done",
                    diagnosis_json=json.dumps(diagnosis, ensure_ascii=False),
                )
                try:  # 卡片尽力而为:诊断已落库,发卡失败不回滚
                    card = build_diagnosis_card(event, diagnosis, now_str=now_str)
                    await patrol_feishu.send(card)
                except Exception:
                    logger.exception("diagnosis card send failed (event %d)", event.id)
            else:
                store.set_diagnosis(
                    event.id,
                    status="failed",
                    diagnosis_json=json.dumps({"raw": raw[:2000]}, ensure_ascii=False),
                )
                logger.warning("event %d diagnosis failed after %d attempts", event.id, attempt)

    async def report_tick() -> None:
        now_ts, now_str, now_local = _now()
        if not report_due(store, now_local, settings.sentinel_report_hour):
            return
        try:  # hygiene 随日报到点先跑,失败不挡日报(当日 hygiene 即放弃,次日随门控重跑;
            # prom 故障另有 metrics_tick 升级兜底);事件先发,日报里即可见
            findings, evaluated, hold = await run_hygiene(prom, settings, now_ts=now_ts)
            await apply_findings(
                findings,
                scope=evaluated,
                store=store,
                feishu=patrol_feishu,
                now_ts=now_ts,
                now_str=now_str,
                cooldown_seconds=cooldown_seconds,
                hold=hold,
            )
        except Exception:
            logger.exception("hygiene failed")
        sent = await run_daily_report(
            store=store,
            feishu=patrol_feishu,
            services=service_names,
            now_local=now_local,
            hour=settings.sentinel_report_hour,
            retention_days=settings.sentinel_probe_retention_days,
        )
        if sent and driver.enabled:
            try:  # AI 总结跟在日报后面,失败只损失总结不影响日报本身
                date_str = now_local.date().isoformat()
                stats = build_daily_stats(store, service_names, now_ts=now_ts, date_str=date_str)
                data = {
                    "date": date_str,
                    "services": [asdict(s) for s in stats],
                    "open_events": [asdict(e) for e in store.get_open_events()],
                    "resolved_24h": store.count_resolved_since(now_ts - 24 * 3600),
                }
                text = await driver.summarize(data)
                if text:
                    card = build_summary_card(text[:1000], date_str=date_str, now_str=now_str)
                    await patrol_feishu.send(card)
            except Exception:
                logger.exception("daily AI summary failed")

    # 分层装配:services 文件缺失→无内部探针;prometheus/loki URL 留空→对应扫描关闭。
    # 最小模式(只填飞书 webhook)也能干净起来,不产生数据源故障噪音。
    jobs: list[tuple[str, float, Tick]] = [
        ("statuspage", settings.sentinel_poll_interval, statuspage_tick)
    ]
    if targets:
        jobs.append(("internal_probe", settings.sentinel_probe_interval, probe_tick))
    jobs.append(("daily_report", _REPORT_CHECK_INTERVAL, report_tick))
    if settings.sentinel_prometheus_url:
        jobs.append(("metrics_scan", settings.sentinel_scan_interval, metrics_tick))
    if settings.sentinel_loki_url:
        jobs.append(("log_scan", settings.sentinel_scan_interval, logs_tick))
    if driver.enabled:
        jobs.append(("diagnosis", settings.sentinel_diag_interval, diag_tick))
    return jobs


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings()
    client = httpx.AsyncClient()
    store = Store(settings.sentinel_db_path)
    try:
        jobs = build_jobs(settings, client, store)
    except Exception:
        # 启动期组装失败(如 services.yaml 缺失):收尾资源后让进程带着清晰报错退出
        await client.aclose()
        store.close()
        raise
    # Phase 3 API 依赖注入:编排脚本经宿主机 127.0.0.1:8765 调用。
    # patrol_feishu 与 build_jobs 闭包内实例是两份(节流互不感知):诊断卡/总结卡
    # 频次极低(<10/月+1/天),叠加突破飞书频控的风险可忽略,不为此重构 build_jobs。
    app.state.settings = settings
    app.state.store = store
    app.state.patrol_feishu = FeishuClient(
        client, settings.patrol_webhook, secret=settings.patrol_sign_secret
    )
    # load_targets 在此与 build_jobs 各调一次(读同一 yaml,幂等),可接受
    app.state.services = [t.name for t in _load_targets_or_disable(settings.sentinel_services_file)]
    logger.info("sentinel started, jobs=%s", [name for name, _, _ in jobs])
    tasks = [asyncio.create_task(_job_loop(name, interval, tick)) for name, interval, tick in jobs]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await client.aclose()
        store.close()


app = FastAPI(title="WatchMend", lifespan=lifespan)

register_routes(app)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
