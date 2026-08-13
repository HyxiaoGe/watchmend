from types import SimpleNamespace

import httpx

from sentinel.app import build_jobs, health
from sentinel.config import Settings
from sentinel.store import Store


class FakeResetMonitor:
    async def tick(self):
        return None

    def health(self):
        return {
            "status": "degraded",
            "last_success_ts": 123,
            "fresh_source_families": 1,
            "sources": [],
        }


async def test_enabled_reset_job_is_independent_and_runs_every_60_seconds(tmp_path):
    settings = Settings(
        _env_file=None,
        feishu_vendor_webhook="https://open.feishu.cn/hook/T",
        sentinel_db_path=str(tmp_path / "app.db"),
        sentinel_services_file=str(tmp_path / "missing-services.yaml"),
        sentinel_codex_reset_enabled=True,
        sentinel_codex_reset_poll_seconds=60,
    )
    monitor = FakeResetMonitor()
    client = httpx.AsyncClient()
    store = Store(settings.sentinel_db_path)
    jobs = build_jobs(settings, client, store, reset_monitor=monitor)

    reset_jobs = [(interval, tick) for name, interval, tick in jobs if name == "codex_reset"]
    assert reset_jobs == [(60, monitor.tick)]
    store.close()
    await client.aclose()


async def test_health_exposes_reset_freshness_without_changing_global_status():
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(reset_monitor=FakeResetMonitor()))
    )
    assert await health(request) == {
        "status": "ok",
        "codex_reset": {
            "status": "degraded",
            "last_success_ts": 123,
            "fresh_source_families": 1,
            "sources": [],
        },
    }
