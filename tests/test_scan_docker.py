# tests/test_scan_docker.py
import pytest

from sentinel.scan_docker import run_docker_scan


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv("SENTINEL_DOCKER_HOST", "tcp://docker-proxy:2375")
    monkeypatch.setenv("SENTINEL_DOCKER_SCAN_INTERVAL", "60")
    from sentinel.config import Settings

    return Settings(_env_file=None)


def _ps_row(name, image="someorg/app:1.0"):
    """A /containers/json row as docker returns it (leading-slash name)."""
    return {"Names": [f"/{name}"], "Image": image}


def _inspect(
    *,
    name,
    image="someorg/app:1.0",
    restart_policy="always",
    status="running",
    restarting=False,
    exit_code=0,
    oom=False,
    error="",
    started_at="2026-06-13T00:00:00Z",
    finished_at="0001-01-01T00:00:00Z",
    health=None,
    restart_count=0,
):
    """A WHITELIST inspect_safe() dict (matches docker_client contract shape)."""
    return {
        "Name": f"/{name}",
        "Image": image,
        "RestartCount": restart_count,
        "RestartPolicy": restart_policy,
        "State": {
            "Status": status,
            "Running": status == "running",
            "Restarting": restarting,
            "ExitCode": exit_code,
            "OOMKilled": oom,
            "Error": error,
            "StartedAt": started_at,
            "FinishedAt": finished_at,
            "Health": health,
        },
        "Env": [],
    }


class FakeDocker:
    """Stand-in for DockerClient: serves canned ps rows + inspect dicts.

    inspects maps name -> dict (returned) OR Exception instance (raised by
    inspect_safe to simulate a per-container failure). ps_error, if set, is
    raised by ps() to simulate a data-source failure.
    """

    def __init__(self, ps_rows, inspects, *, ps_error=None):
        self._ps_rows = ps_rows
        self._inspects = inspects
        self._ps_error = ps_error

    async def ps(self, *, all=True):
        if self._ps_error is not None:
            raise self._ps_error
        return self._ps_rows

    async def inspect_safe(self, name):
        val = self._inspects[name]
        if isinstance(val, Exception):
            raise val
        return val


async def _scan(settings, ps_rows, inspects, *, now_ts=1_780_000_000, emit_oom=True):
    docker = FakeDocker(ps_rows, inspects)
    return await run_docker_scan(docker, settings, now_ts=now_ts, emit_oom=emit_oom)


# A recent FinishedAt relative to now_ts=1_780_000_000 (epoch -> RFC3339 UTC).
# 1_780_000_000 - 30 = 1_779_999_970 -> "2026-05-28T20:26:10Z" (UTC), within the
# OOM window of max(2*60, 300) = 300s. See the compute step below for verification.
_RECENT_FINISHED = "2026-05-28T20:26:10Z"  # = 1_779_999_970 UTC


# --- container_down: restart-policy filtering ---


async def test_always_exited_fires_down(settings):
    findings, active = await _scan(
        settings,
        [_ps_row("web")],
        {"web": _inspect(name="web", restart_policy="always", status="exited", exit_code=1)},
    )
    assert [(f.rule, f.subject, f.severity) for f in findings] == [
        ("container_down", "web", "critical")
    ]
    assert findings[0].needs_diagnosis and not findings[0].point
    assert findings[0].payload["exit_code"] == 1
    assert active == {"web"}


async def test_restart_no_exited_filtered_out(settings):
    findings, active = await _scan(
        settings,
        [_ps_row("batch")],
        {"batch": _inspect(name="batch", restart_policy="no", status="exited", exit_code=1)},
    )
    assert findings == []
    assert active == set()  # batch/one-shot containers are not observed


async def test_on_failure_exit0_no_finding(settings):
    findings, active = await _scan(
        settings,
        [_ps_row("job")],
        {"job": _inspect(name="job", restart_policy="on-failure", status="exited", exit_code=0)},
    )
    assert findings == []
    assert active == {"job"}  # observed (policy eligible), just not down


async def test_on_failure_exit1_fires_down(settings):
    findings, active = await _scan(
        settings,
        [_ps_row("job")],
        {"job": _inspect(name="job", restart_policy="on-failure", status="exited", exit_code=1)},
    )
    assert [(f.rule, f.subject) for f in findings] == [("container_down", "job")]
    assert active == {"job"}


# --- container_unhealthy ---


async def test_unhealthy_fires_warning(settings):
    findings, active = await _scan(
        settings,
        [_ps_row("api")],
        {"api": _inspect(name="api", status="running", health={"Status": "unhealthy"})},
    )
    assert [(f.rule, f.subject, f.severity) for f in findings] == [
        ("container_unhealthy", "api", "warning")
    ]
    assert findings[0].needs_diagnosis and not findings[0].point
    assert active == {"api"}


async def test_no_health_no_finding(settings):
    findings, active = await _scan(
        settings,
        [_ps_row("api")],
        {"api": _inspect(name="api", status="running", health=None)},
    )
    assert findings == []
    assert active == {"api"}


async def test_health_starting_no_finding(settings):
    findings, active = await _scan(
        settings,
        [_ps_row("api")],
        {"api": _inspect(name="api", status="running", health={"Status": "starting"})},
    )
    assert findings == []
    assert active == {"api"}


# --- container_oom ---


async def test_oom_in_window_emit_true_fires_point(settings):
    findings, active = await _scan(
        settings,
        [_ps_row("worker")],
        {
            "worker": _inspect(
                name="worker",
                status="exited",
                oom=True,
                exit_code=137,
                finished_at=_RECENT_FINISHED,
            )
        },
    )
    rules = {(f.rule, f.subject, f.severity, f.point) for f in findings}
    # exited+always also yields a container_down here; we only assert the OOM finding is present.
    assert ("container_oom", "worker", "critical", True) in rules
    assert active == {"worker"}


async def test_oom_emit_false_no_oom_finding(settings):
    findings, active = await _scan(
        settings,
        [_ps_row("worker")],
        {
            "worker": _inspect(
                name="worker",
                status="exited",
                oom=True,
                exit_code=137,
                finished_at=_RECENT_FINISHED,
            )
        },
        emit_oom=False,
    )
    assert not any(f.rule == "container_oom" for f in findings)
    assert active == {"worker"}


async def test_oom_stale_finished_at_no_finding(settings):
    findings, active = await _scan(
        settings,
        [_ps_row("worker")],
        {
            "worker": _inspect(
                name="worker",
                status="exited",
                oom=True,
                exit_code=137,
                finished_at="2020-01-01T00:00:00Z",  # long ago, outside window
            )
        },
    )
    assert not any(f.rule == "container_oom" for f in findings)


# --- filtering: exclude list, self-exclude, removing ---


async def test_exclude_list_skips(settings, monkeypatch):
    monkeypatch.setenv("SENTINEL_DOCKER_EXCLUDE", "skipme")
    from sentinel.config import Settings

    s = Settings(_env_file=None)
    findings, active = await _scan(
        s,
        [_ps_row("skipme")],
        {"skipme": _inspect(name="skipme", status="exited", exit_code=1)},
    )
    assert findings == []
    assert active == set()


async def test_self_exclude_watchmend_image(settings):
    findings, active = await _scan(
        settings,
        [_ps_row("watchmend", image="ghcr.io/example/watchmend:0.1.1")],
        {
            "watchmend": _inspect(
                name="watchmend",
                status="exited",
                exit_code=1,
                image="ghcr.io/example/watchmend:0.1.1",
            )
        },
    )
    assert findings == []
    assert active == set()


async def test_self_exclude_socket_proxy_image(settings):
    findings, active = await _scan(
        settings,
        [_ps_row("docker-proxy", image="tecnativa/docker-socket-proxy:0.1")],
        {
            "docker-proxy": _inspect(
                name="docker-proxy",
                status="exited",
                exit_code=1,
                image="tecnativa/docker-socket-proxy:0.1",
            )
        },
    )
    assert findings == []
    assert active == set()


async def test_removing_skipped_not_in_active(settings):
    findings, active = await _scan(
        settings,
        [_ps_row("gone")],
        {"gone": _inspect(name="gone", status="removing")},
    )
    assert findings == []
    assert active == set()  # treated as going away


# --- active_subjects content + failure tolerance + ps propagation ---


async def test_active_subjects_only_evaluated_containers(settings):
    findings, active = await _scan(
        settings,
        [_ps_row("up"), _ps_row("paused-one"), _ps_row("created-one")],
        {
            "up": _inspect(name="up", status="running"),
            "paused-one": _inspect(name="paused-one", status="paused"),
            "created-one": _inspect(name="created-one", status="created"),
        },
    )
    assert findings == []  # created/paused -> observed, no finding
    assert active == {"up", "paused-one", "created-one"}


async def test_inspect_failure_skipped_not_in_active_no_abort(settings):
    findings, active = await _scan(
        settings,
        [_ps_row("bad"), _ps_row("good")],
        {
            "bad": RuntimeError("inspect boom"),
            "good": _inspect(name="good", status="exited", exit_code=1),
        },
    )
    # scan continues past the failing container; good still evaluated
    assert [(f.rule, f.subject) for f in findings] == [("container_down", "good")]
    assert active == {"good"}  # bad not added


async def test_ps_failure_propagates(settings):
    docker = FakeDocker([], {}, ps_error=RuntimeError("ps boom"))
    with pytest.raises(RuntimeError, match="ps boom"):
        await run_docker_scan(docker, settings, now_ts=1_780_000_000, emit_oom=True)


async def test_dead_status_fires_down(settings):
    # docker 的 dead 状态(不可恢复)与 exited 同等处理 → container_down
    findings, active = await _scan(
        settings,
        [_ps_row("zombie")],
        {"zombie": _inspect(name="zombie", status="dead", exit_code=255)},
    )
    assert [(f.rule, f.subject, f.severity) for f in findings] == [
        ("container_down", "zombie", "critical")
    ]
    assert active == {"zombie"}


async def test_empty_names_row_skipped(settings):
    # /containers/json 偶发返回无 Names 的行:跳过,不 inspect、不入 active
    findings, active = await _scan(
        settings,
        [{"Names": [], "Image": "someorg/app:1.0"}],
        {},
    )
    assert findings == []
    assert active == set()


async def test_oom_future_finished_at_no_finding(settings):
    # 时钟偏移导致 FinishedAt 在 now_ts 之后:差为负,0<= 下界拦截,不得误触 OOM 点卡
    findings, active = await _scan(
        settings,
        [_ps_row("worker")],
        {
            "worker": _inspect(
                name="worker",
                status="exited",
                oom=True,
                exit_code=137,
                finished_at="2027-01-01T00:00:00Z",  # future relative to now_ts=1_780_000_000
            )
        },
    )
    assert not any(f.rule == "container_oom" for f in findings)
