# tests/test_discover.py
from sentinel import discover


def _settings(monkeypatch, **env):
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from sentinel.config import Settings

    return Settings(_env_file=None)


class FakeDocker:
    """最小 DockerClient 替身:只实现 probe 需要的 async ps()。"""

    def __init__(self, rows, *, raises=False):
        self._rows = rows
        self._raises = raises

    async def ps(self, *, all=True):
        if self._raises:
            raise RuntimeError("docker socket boom")
        return self._rows


async def test_probe_suggests_prometheus_when_url_empty(monkeypatch):
    settings = _settings(monkeypatch, SENTINEL_PROMETHEUS_URL="")
    docker = FakeDocker([{"Names": ["/prom"], "Image": "prom/prometheus:v2"}])

    out = await discover.probe(docker, settings)

    assert len(out) == 1
    msg = out[0]
    assert "SENTINEL_PROMETHEUS_URL" in msg
    assert "prom" in msg  # 命中容器名进入建议


async def test_probe_silent_when_prometheus_url_set(monkeypatch):
    settings = _settings(monkeypatch, SENTINEL_PROMETHEUS_URL="http://prometheus:9090")
    docker = FakeDocker([{"Names": ["/prom"], "Image": "prom/prometheus:v2"}])

    out = await discover.probe(docker, settings)

    assert out == []


async def test_probe_returns_empty_when_docker_none(monkeypatch):
    settings = _settings(monkeypatch)

    out = await discover.probe(None, settings)

    assert out == []


async def test_probe_swallows_ps_failure(monkeypatch):
    settings = _settings(monkeypatch, SENTINEL_PROMETHEUS_URL="")
    docker = FakeDocker([], raises=True)

    out = await discover.probe(docker, settings)

    assert out == []  # ps 抛错不崩溃,返回空建议列表


async def test_probe_suggests_loki_when_url_empty(monkeypatch):
    # 第二条指纹(Loki)也要走通,防 FINGERPRINTS tuple 笔误
    settings = _settings(monkeypatch, SENTINEL_PROMETHEUS_URL="", SENTINEL_LOKI_URL="")
    docker = FakeDocker([{"Names": ["/loki"], "Image": "grafana/loki:2.9"}])

    out = await discover.probe(docker, settings)

    assert len(out) == 1
    msg = out[0]
    assert "SENTINEL_LOKI_URL" in msg
    assert ":3100" in msg  # Loki 默认端口进入建议


async def test_probe_skips_unnamed_container(monkeypatch):
    # 无名容器跳过:不能渲染出 http://:9090 这种坏 URL
    settings = _settings(monkeypatch, SENTINEL_PROMETHEUS_URL="")
    docker = FakeDocker([{"Names": [], "Image": "prom/prometheus:v2"}])

    out = await discover.probe(docker, settings)

    assert out == []


async def test_lifespan_invokes_discover_probe(tmp_path, monkeypatch):
    # discover.probe 在 lifespan 启动期被调用一次(MVP:建议打到日志)。
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("SENTINEL_SERVICES_FILE", str(tmp_path / "no-such.yaml"))

    import sentinel.app as app_mod

    called: list[tuple] = []

    async def _fake_probe(docker, settings):
        called.append((docker, settings))
        return ["💡 test suggestion"]

    monkeypatch.setattr(app_mod.discover, "probe", _fake_probe)
    monkeypatch.setattr(app_mod, "build_jobs", lambda *a, **k: [])  # 不起任何 job 循环

    app = app_mod.FastAPI()
    async with app_mod.lifespan(app):
        pass

    assert len(called) == 1  # 启动期恰好调用一次
