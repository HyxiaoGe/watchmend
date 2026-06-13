# tests/test_docker_client.py
import json

import httpx
import pytest
import respx

from sentinel.docker_client import (
    DockerClient,
    _check_name,
    _demux_docker_logs,
    parse_docker_time,
)


def test_unix_endpoint_uses_uds_transport_and_placeholder_base_url():
    dc = DockerClient("unix:///var/run/docker.sock")
    transport = dc._client._transport
    assert isinstance(transport, httpx.AsyncHTTPTransport)
    # uds 套接字路径来自 partition("://") 之后的 rest(httpx 0.28.1 验证)
    assert transport._pool._uds == "/var/run/docker.sock"
    assert str(dc._client.base_url) == "http://docker"


def test_tcp_endpoint_rewrites_to_http_not_fed_raw():
    dc = DockerClient("tcp://docker-proxy:2375")
    # 关键:tcp:// 不能原样喂 httpx,必须转成 http://
    assert str(dc._client.base_url) == "http://docker-proxy:2375"
    assert "tcp://" not in str(dc._client.base_url)


def test_https_endpoint_rejected_no_tls():
    # 本类无 TLS transport:https 不能静默降级成 http(否则明文外泄),必须明确拒绝
    with pytest.raises(ValueError, match="https docker endpoint is not supported"):
        DockerClient("https://h:2376")


def test_unknown_scheme_raises_value_error():
    with pytest.raises(ValueError, match="unsupported docker endpoint"):
        DockerClient("ftp://nope")


async def test_ps_all_issues_all_1_and_returns_list():
    dc = DockerClient("tcp://docker-proxy:2375")
    rows = [{"Names": ["/api"], "Status": "Up", "Image": "api:1"}]
    with respx.mock:
        route = respx.get("http://docker-proxy:2375/containers/json").mock(
            return_value=httpx.Response(200, json=rows)
        )
        out = await dc.ps(all=True)
    await dc.aclose()
    assert out == rows
    assert route.calls[0].request.url.params["all"] == "1"


async def test_ps_not_all_omits_all_param():
    dc = DockerClient("tcp://docker-proxy:2375")
    with respx.mock:
        route = respx.get("http://docker-proxy:2375/containers/json").mock(
            return_value=httpx.Response(200, json=[])
        )
        await dc.ps(all=False)
    await dc.aclose()
    assert "all" not in route.calls[0].request.url.params


async def test_inspect_safe_whitelist_drops_secrets_and_host_paths():
    payload = {
        "Name": "/api",
        "RestartCount": 3,
        "Config": {"Image": "api:1", "Env": ["DB_PASSWORD=hunter2", "MODE=prod"]},
        "Args": ["--password=sekret"],
        "HostConfig": {
            "RestartPolicy": {"Name": "always"},
            "Binds": ["/host/secret:/x"],
        },
        "Mounts": [{"Source": "/host/y"}],
        "NetworkSettings": {"IPAddress": "10.0.0.9"},
        "State": {
            "Status": "running",
            "Running": True,
            "Restarting": False,
            "ExitCode": 0,
            "OOMKilled": False,
            "Error": "",
            "StartedAt": "2026-06-13T00:00:00Z",
            "FinishedAt": "0001-01-01T00:00:00Z",
            "Health": {"Status": "healthy"},
        },
    }
    dc = DockerClient("unix:///var/run/docker.sock")
    with respx.mock:
        respx.get("http://docker/containers/api/json").mock(
            return_value=httpx.Response(200, json=payload)
        )
        out = await dc.inspect_safe("api")
    await dc.aclose()
    # Env 只留变量名,值全部丢弃
    assert out["Env"] == ["DB_PASSWORD", "MODE"]
    # 白名单字段在
    assert out["Name"] == "/api"
    assert out["Image"] == "api:1"
    assert out["RestartCount"] == 3
    assert out["RestartPolicy"] == "always"
    assert out["State"]["Status"] == "running"
    assert out["State"]["Health"] == {"Status": "healthy"}
    # 丢弃的字段一律不出现在白名单结果里
    for dropped in ("Args", "HostConfig", "Mounts", "NetworkSettings"):
        assert dropped not in out
    # 任何密钥/宿主路径都不得出现在序列化结果里
    blob = json.dumps(out, ensure_ascii=False)
    for leak in ("hunter2", "prod", "sekret", "/host/secret", "/host/y", "10.0.0.9"):
        assert leak not in blob


async def test_inspect_safe_missing_fields_default_safely():
    # 容器无 HEALTHCHECK / 无 Env / 无 RestartPolicy 时不抛
    payload = {"Name": "/bare", "Config": {}, "State": {"Status": "running"}}
    dc = DockerClient("unix:///var/run/docker.sock")
    with respx.mock:
        respx.get("http://docker/containers/bare/json").mock(
            return_value=httpx.Response(200, json=payload)
        )
        out = await dc.inspect_safe("bare")
    await dc.aclose()
    assert out["Env"] == []
    assert out["Image"] is None
    assert out["RestartCount"] is None
    assert out["RestartPolicy"] is None
    assert out["State"]["Health"] is None


async def test_inspect_safe_health_log_output_not_leaked():
    # Health.Log[].Output 是 healthcheck 命令的原始输出,常含密钥/宿主路径,必须丢弃
    payload = {
        "Name": "/db",
        "Config": {"Image": "db:1"},
        "State": {
            "Status": "running",
            "Health": {
                "Status": "unhealthy",
                "FailingStreak": 3,
                "Log": [
                    {"Output": "connect failed: password=hunter2 host=/host/db.sock", "ExitCode": 1}
                ],
            },
        },
    }
    dc = DockerClient("unix:///var/run/docker.sock")
    with respx.mock:
        respx.get("http://docker/containers/db/json").mock(
            return_value=httpx.Response(200, json=payload)
        )
        out = await dc.inspect_safe("db")
    await dc.aclose()
    # 只保留 Health.Status;Log / Output / FailingStreak 整段丢弃
    assert out["State"]["Health"] == {"Status": "unhealthy"}
    blob = json.dumps(out, ensure_ascii=False)
    for leak in ("hunter2", "/host/db.sock", "FailingStreak", "Output"):
        assert leak not in blob


async def test_logs_demuxes_framed_and_raw():
    line1 = b"hello\n"
    line2 = b"world\n"
    framed = (
        b"\x01\x00\x00\x00"
        + len(line1).to_bytes(4, "big")
        + line1
        + b"\x02\x00\x00\x00"
        + len(line2).to_bytes(4, "big")
        + line2
    )
    dc = DockerClient("unix:///var/run/docker.sock")
    with respx.mock:
        respx.get("http://docker/containers/api/logs").mock(
            return_value=httpx.Response(200, content=framed)
        )
        out = await dc.logs("api")
    await dc.aclose()
    assert out == "hello\nworld\n"


async def test_logs_tail_clamps_high_and_low():
    dc = DockerClient("tcp://docker-proxy:2375")
    with respx.mock:
        route = respx.get("http://docker-proxy:2375/containers/api/logs").mock(
            return_value=httpx.Response(200, content=b"x")
        )
        await dc.logs("api", tail=9999)  # >500 钳到 500
        await dc.logs("api", tail=0)  # <1 钳到 1
    await dc.aclose()
    assert route.calls[0].request.url.params["tail"] == "500"
    assert route.calls[1].request.url.params["tail"] == "1"


async def test_logs_rejects_injection_name():
    dc = DockerClient("unix:///var/run/docker.sock")
    with pytest.raises(ValueError, match="invalid container name"):
        await dc.logs("../etc/passwd")
    await dc.aclose()


def test_demux_docker_logs_frames_and_raw():
    line1 = b"hello\n"
    line2 = b"world\n"
    framed = (
        b"\x01\x00\x00\x00"
        + len(line1).to_bytes(4, "big")
        + line1
        + b"\x02\x00\x00\x00"
        + len(line2).to_bytes(4, "big")
        + line2
    )
    assert _demux_docker_logs(framed) == "hello\nworld\n"
    # TTY 模式裸流原样退回
    assert _demux_docker_logs(b"plain text log") == "plain text log"


def test_check_name_rejects_traversal():
    with pytest.raises(ValueError, match="invalid container name"):
        _check_name("../etc/passwd")
    # 合法名不抛
    _check_name("watchmend-api")


def test_parse_docker_time_rfc3339_nano():
    # 9 位纳秒可解析到正确 epoch 秒(纳秒截到微秒,Z→+00:00)
    # 1781308800 由 datetime.fromisoformat 实测得出,勿手改
    assert parse_docker_time("2026-06-13T00:00:00.123456789Z") == 1781308800


def test_parse_docker_time_zero_value_and_garbage():
    assert parse_docker_time("0001-01-01T00:00:00Z") is None
    assert parse_docker_time("") is None
    assert parse_docker_time("garbage") is None
