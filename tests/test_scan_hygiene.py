# tests/test_scan_hygiene.py
import os
import time

import httpx
import pytest
import respx

from sentinel.promql import PromClient
from sentinel.scan_hygiene import check_backup, check_certs, run_hygiene

NOW = int(time.time())


@pytest.fixture
def settings(monkeypatch, tmp_path):
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv("SENTINEL_PROMETHEUS_URL", "http://prom.test")
    monkeypatch.setenv("SENTINEL_BACKUP_DIR", str(tmp_path / "pg"))
    monkeypatch.setenv("SENTINEL_CERT_DOMAINS", "a.test")
    from sentinel.config import Settings

    return Settings(_env_file=None)


def _touch(path, age_hours):
    path.write_bytes(b"x")
    mtime = NOW - age_hours * 3600
    os.utime(path, (mtime, mtime))


def test_backup_fresh_quiet(tmp_path):
    d = tmp_path / "pg"
    d.mkdir()
    _touch(d / "pg_20260611.sql.gz", age_hours=7)
    assert check_backup(str(d), 36, now_ts=NOW) == []


def test_backup_stale_fires(tmp_path):
    d = tmp_path / "pg"
    d.mkdir()
    _touch(d / "pg_20260608.sql.gz", age_hours=80)
    _touch(d / "pg_20260607.sql.gz", age_hours=104)
    findings = check_backup(str(d), 36, now_ts=NOW)
    assert [(f.rule, f.subject, f.severity) for f in findings] == [
        ("backup_stale", "pg", "critical")
    ]
    assert "pg_20260608" in findings[0].detail  # 报最新的那个


def test_backup_missing_dir_skips_as_disabled(tmp_path):
    # 开箱即用:没挂备份目录的部署视为未启用,返回 None 而非误报 critical
    assert check_backup(str(tmp_path / "nope"), 36, now_ts=NOW) is None


def test_backup_empty_dir_skips_as_disabled(tmp_path):
    d = tmp_path / "pg"
    d.mkdir()
    assert check_backup(str(d), 36, now_ts=NOW) is None


async def test_cert_expiring_fires(monkeypatch):
    import sentinel.scan_hygiene as sh

    monkeypatch.setattr(sh, "_cert_expiry_ts", lambda domain: NOW + 5 * 86400)
    findings, hold = await check_certs(["a.test"], 14, now_ts=NOW)
    assert [(f.rule, f.subject) for f in findings] == [("cert_expiry", "a.test")]
    assert "5.0 天" in findings[0].detail
    assert hold == set()


async def test_cert_fresh_quiet(monkeypatch):
    import sentinel.scan_hygiene as sh

    monkeypatch.setattr(sh, "_cert_expiry_ts", lambda domain: NOW + 60 * 86400)
    findings, hold = await check_certs(["a.test"], 14, now_ts=NOW)
    assert findings == []
    assert hold == set()


async def test_cert_check_failure_holds_not_recovers(monkeypatch):
    import sentinel.scan_hygiene as sh

    def boom(domain):
        raise OSError("unreachable")

    monkeypatch.setattr(sh, "_cert_expiry_ts", boom)
    findings, hold = await check_certs(["a.test"], 14, now_ts=NOW)
    assert findings == []  # 连不上≠临期
    assert hold == {("cert_expiry", "a.test")}  # 但也≠恢复:open 事件被 hold 保护


async def test_run_hygiene_forecast_failure_drops_rule_from_scope(settings, monkeypatch):
    import sentinel.scan_hygiene as sh

    monkeypatch.setattr(sh, "_cert_expiry_ts", lambda domain: NOW + 60 * 86400)
    with respx.mock:
        respx.get("http://prom.test/api/v1/query").mock(return_value=httpx.Response(500))
        async with httpx.AsyncClient() as client:
            findings, evaluated, hold = await run_hygiene(
                PromClient(client, "http://prom.test"), settings, now_ts=NOW
            )
    # forecast 失败被剔除;备份目录不存在=未启用,backup_stale 同样不进 scope
    assert evaluated == {"cert_expiry"}
    assert findings == []


async def test_run_hygiene_all_green(settings, monkeypatch, tmp_path):
    import sentinel.scan_hygiene as sh

    d = tmp_path / "pg"
    d.mkdir()
    _touch(d / "pg_x.sql.gz", age_hours=7)
    monkeypatch.setenv("SENTINEL_BACKUP_DIR", str(d))
    from sentinel.config import Settings

    settings = Settings(_env_file=None)
    monkeypatch.setattr(sh, "_cert_expiry_ts", lambda domain: NOW + 60 * 86400)
    forecast = {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": {}, "value": [0, "1e11"]}],
        },
    }
    with respx.mock:
        respx.get("http://prom.test/api/v1/query").mock(
            return_value=httpx.Response(200, json=forecast)
        )
        async with httpx.AsyncClient() as client:
            findings, evaluated, hold = await run_hygiene(
                PromClient(client, "http://prom.test"), settings, now_ts=NOW
            )
    assert findings == []
    assert evaluated == {"backup_stale", "disk_forecast", "cert_expiry"}
    assert hold == set()


async def test_run_hygiene_forecast_insufficient_history_not_evaluated(settings, monkeypatch):
    # 新装机:24h 前无样本 → 跳过预测且不进 evaluated。即便 predict_linear 已被
    # 装机写盘斜率带成负值(必"写满"),守卫也要拦住这张开箱误报卡
    import sentinel.scan_hygiene as sh

    monkeypatch.setattr(sh, "_cert_expiry_ts", lambda domain: NOW + 60 * 86400)
    empty = {"status": "success", "data": {"resultType": "vector", "result": []}}
    doomed = {
        "status": "success",
        "data": {"resultType": "vector", "result": [{"metric": {}, "value": [0, "-1e12"]}]},
    }

    def route(request):
        q = request.url.params["query"]
        return httpx.Response(200, json=empty if "offset" in q else doomed)

    with respx.mock:
        respx.get("http://prom.test/api/v1/query").mock(side_effect=route)
        async with httpx.AsyncClient() as client:
            findings, evaluated, hold = await run_hygiene(
                PromClient(client, "http://prom.test"), settings, now_ts=NOW
            )
    assert findings == []
    assert "disk_forecast" not in evaluated


async def test_run_hygiene_forecast_fires_with_history(settings, monkeypatch):
    # 历史充足且外推为负 → 正常告警(守卫只拦新装机,不拦真问题)
    import sentinel.scan_hygiene as sh

    monkeypatch.setattr(sh, "_cert_expiry_ts", lambda domain: NOW + 60 * 86400)
    doomed = {
        "status": "success",
        "data": {"resultType": "vector", "result": [{"metric": {}, "value": [0, "-1e12"]}]},
    }
    with respx.mock:
        respx.get("http://prom.test/api/v1/query").mock(
            return_value=httpx.Response(200, json=doomed)
        )
        async with httpx.AsyncClient() as client:
            findings, evaluated, hold = await run_hygiene(
                PromClient(client, "http://prom.test"), settings, now_ts=NOW
            )
    assert [(f.rule, f.subject) for f in findings] == [("disk_forecast", "/")]
    assert "disk_forecast" in evaluated


def test_backup_zero_byte_file_still_fires(tmp_path):
    # 目录里有文件但全是 0 字节=备份产物坏了:与"未启用"(空目录)不同,必须告警
    d = tmp_path / "pg"
    d.mkdir()
    (d / "pg_fail.sql.gz").write_bytes(b"")
    os.utime(d / "pg_fail.sql.gz", (NOW - 3600, NOW - 3600))
    findings = check_backup(str(d), 36, now_ts=NOW)
    assert findings[0].severity == "critical"
    assert "无有效备份" in findings[0].detail


async def test_run_hygiene_prometheus_disabled_skips_forecast(monkeypatch, tmp_path):
    # SENTINEL_PROMETHEUS_URL 留空=指标层未接入:forecast 不评估也不报错
    import sentinel.scan_hygiene as sh

    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv("SENTINEL_PROMETHEUS_URL", "")
    monkeypatch.setenv("SENTINEL_CERT_DOMAINS", "a.test")
    monkeypatch.setenv("SENTINEL_BACKUP_DIR", str(tmp_path / "nope"))
    from sentinel.config import Settings

    settings = Settings(_env_file=None)
    monkeypatch.setattr(sh, "_cert_expiry_ts", lambda domain: NOW + 60 * 86400)
    async with httpx.AsyncClient() as client:
        findings, evaluated, hold = await run_hygiene(PromClient(client, ""), settings, now_ts=NOW)
    assert findings == []
    assert evaluated == {"cert_expiry"}


async def test_run_hygiene_cert_domains_empty_not_evaluated(monkeypatch, tmp_path):
    # SENTINEL_CERT_DOMAINS 留空(开箱默认)=证书检查未启用:cert_expiry 不进 evaluated,
    # 否则清空域名列表的下一轮 hygiene 会把 open 的证书事件假恢复(不评估≠恢复)
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv("SENTINEL_PROMETHEUS_URL", "")
    monkeypatch.setenv("SENTINEL_CERT_DOMAINS", "")
    monkeypatch.setenv("SENTINEL_BACKUP_DIR", str(tmp_path / "nope"))
    from sentinel.config import Settings

    settings = Settings(_env_file=None)
    async with httpx.AsyncClient() as client:
        findings, evaluated, hold = await run_hygiene(PromClient(client, ""), settings, now_ts=NOW)
    assert findings == []
    assert evaluated == set()  # 全最小模式:一项都没评估,scope 为空,任何 open 事件都不动
    assert hold == set()
