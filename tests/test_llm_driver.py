# tests/test_llm_driver.py
import json

import httpx
import respx

from sentinel.docker_client import DockerClient
from sentinel.findings import EventRecord
from sentinel.llm_driver import LLMDriver, parse_diagnosis

LLM_URL = "http://llm.test/v1/chat/completions"

DIAG_JSON = {
    "summary": "内存高",
    "root_cause": "泄漏",
    "evidence": ["rss 持续增长"],
    "confidence": "medium",
}
FINAL_TEXT = "结论:\n```json\n" + json.dumps(DIAG_JSON, ensure_ascii=False) + "\n```"


def _settings(monkeypatch, **env):
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from sentinel.config import Settings

    return Settings(_env_file=None)


def _event() -> EventRecord:
    return EventRecord(
        id=1,
        ts=1700000000,
        rule="mem_pressure",
        subject="api",
        severity="warning",
        status="open",
        detail="容器 api 内存连续 15 分钟超 limit",
        payload_json="{}",
        diagnosis_status="pending",
        diagnosis_json=None,
        cooldown_until=0,
        resolved_ts=None,
    )


def _llm_message(content=None, tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return httpx.Response(200, json={"choices": [{"message": msg}]})


def test_disabled_without_config(monkeypatch):
    settings = _settings(monkeypatch)
    driver = LLMDriver(httpx.AsyncClient(), settings)
    assert not driver.enabled


def test_enabled_needs_both_url_and_model(monkeypatch):
    settings = _settings(monkeypatch, LLM_BASE_URL="http://llm.test/v1")
    assert not LLMDriver(httpx.AsyncClient(), settings).enabled
    settings = _settings(monkeypatch, LLM_BASE_URL="http://llm.test/v1", LLM_MODEL="m")
    assert LLMDriver(httpx.AsyncClient(), settings).enabled


async def test_diagnose_runs_tool_loop(monkeypatch):
    settings = _settings(
        monkeypatch, LLM_BASE_URL="http://llm.test/v1", LLM_MODEL="m", LLM_API_KEY="sk-test-key"
    )
    async with httpx.AsyncClient() as client:
        driver = LLMDriver(client, settings)
        with respx.mock:
            llm = respx.post(LLM_URL).mock(
                side_effect=[
                    _llm_message(
                        tool_calls=[
                            {
                                "id": "c1",
                                "type": "function",
                                "function": {
                                    "name": "prom_query",
                                    "arguments": '{"query": "up"}',
                                },
                            }
                        ]
                    ),
                    _llm_message(content=FINAL_TEXT),
                ]
            )
            respx.get("http://prometheus:9090/api/v1/query").mock(
                return_value=httpx.Response(200, json={"status": "success", "data": {}})
            )
            diagnosis, raw = await driver.diagnose(_event())

    assert diagnosis == DIAG_JSON
    assert "```json" in raw
    first = json.loads(llm.calls[0].request.content)
    assert first["model"] == "m"
    assert any(t["function"]["name"] == "prom_query" for t in first["tools"])
    assert llm.calls[0].request.headers["authorization"] == "Bearer sk-test-key"
    # 第二轮请求里必须带工具结果消息,且 tool_call_id 对得上
    second = json.loads(llm.calls[1].request.content)
    tool_msgs = [m for m in second["messages"] if m["role"] == "tool"]
    assert tool_msgs and tool_msgs[0]["tool_call_id"] == "c1"
    assert "success" in tool_msgs[0]["content"]


async def test_tool_failure_fed_back_not_raised(monkeypatch):
    # 工具失败(prom 500)不打断诊断:错误文本回给模型继续推理
    settings = _settings(monkeypatch, LLM_BASE_URL="http://llm.test/v1", LLM_MODEL="m")
    async with httpx.AsyncClient() as client:
        driver = LLMDriver(client, settings)
        with respx.mock:
            llm = respx.post(LLM_URL).mock(
                side_effect=[
                    _llm_message(
                        tool_calls=[
                            {
                                "id": "c1",
                                "type": "function",
                                "function": {"name": "prom_query", "arguments": '{"query":"up"}'},
                            }
                        ]
                    ),
                    _llm_message(content=FINAL_TEXT),
                ]
            )
            respx.get("http://prometheus:9090/api/v1/query").mock(return_value=httpx.Response(500))
            diagnosis, _ = await driver.diagnose(_event())
    assert diagnosis == DIAG_JSON
    second = json.loads(llm.calls[1].request.content)
    tool_msg = [m for m in second["messages"] if m["role"] == "tool"][0]
    assert tool_msg["content"].startswith("tool prom_query failed")


async def test_unknown_tool_and_bad_args(monkeypatch):
    settings = _settings(monkeypatch, LLM_BASE_URL="http://llm.test/v1", LLM_MODEL="m")
    async with httpx.AsyncClient() as client:
        driver = LLMDriver(client, settings)
        assert (await driver._run_tool("no_such", {})).startswith("unknown tool")
        # 参数名不对 → TypeError 被捕获回喂,不抛
        out = await driver._run_tool("prom_query", {"q": "up"})
        assert out.startswith("tool prom_query failed")


async def test_rounds_exhausted_forces_final_answer(monkeypatch):
    # 模型每轮都要工具:轮数耗尽后撤掉工具催最终结论,循环必然终止
    settings = _settings(
        monkeypatch, LLM_BASE_URL="http://llm.test/v1", LLM_MODEL="m", LLM_MAX_TOOL_ROUNDS="1"
    )
    async with httpx.AsyncClient() as client:
        driver = LLMDriver(client, settings)
        with respx.mock:
            llm = respx.post(LLM_URL).mock(
                side_effect=[
                    _llm_message(
                        tool_calls=[
                            {
                                "id": "c1",
                                "type": "function",
                                "function": {"name": "prom_query", "arguments": '{"query":"up"}'},
                            }
                        ]
                    ),
                    _llm_message(content=FINAL_TEXT),
                ]
            )
            respx.get("http://prometheus:9090/api/v1/query").mock(
                return_value=httpx.Response(200, json={"status": "success"})
            )
            diagnosis, _ = await driver.diagnose(_event())
    assert diagnosis == DIAG_JSON
    final = json.loads(llm.calls[1].request.content)
    assert "tools" not in final  # 催结论那轮不再给工具
    assert final["messages"][-1]["content"].startswith("工具轮数已用完")


async def test_summarize_single_round(monkeypatch):
    settings = _settings(monkeypatch, LLM_BASE_URL="http://llm.test/v1", LLM_MODEL="m")
    async with httpx.AsyncClient() as client:
        driver = LLMDriver(client, settings)
        with respx.mock:
            llm = respx.post(LLM_URL).mock(return_value=_llm_message(content="整体平稳,无异常。"))
            text = await driver.summarize({"date": "2026-06-12", "services": []})
    assert text == "整体平稳,无异常。"
    body = json.loads(llm.calls[0].request.content)
    assert "tools" not in body  # 总结数据全内联,不需要工具
    assert "2026-06-12" in body["messages"][0]["content"]


async def test_loki_logs_formats_lines(monkeypatch):
    settings = _settings(monkeypatch, LLM_BASE_URL="http://llm.test/v1", LLM_MODEL="m")
    payload = {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [
                {
                    "stream": {"container": "api"},
                    "values": [["1700000000000000000", "ERROR boom"]],
                }
            ],
        },
    }
    async with httpx.AsyncClient() as client:
        driver = LLMDriver(client, settings)
        with respx.mock:
            route = respx.get("http://loki:3100/loki/api/v1/query_range").mock(
                return_value=httpx.Response(200, json=payload)
            )
            out = await driver._loki_logs('{container="api"}', minutes=10)
    assert out == "[api] ERROR boom"
    params = route.calls[0].request.url.params
    assert int(params["end"]) - int(params["start"]) == 10 * 60 * 1_000_000_000


def test_docker_tools_absent_without_docker(monkeypatch):
    # docker=None → 三件套既不在 handlers 也不在 specs 里
    settings = _settings(monkeypatch, LLM_BASE_URL="http://llm.test/v1", LLM_MODEL="m")
    driver = LLMDriver(httpx.AsyncClient(), settings, None)
    assert not any(k.startswith("docker") for k in driver._handlers())
    assert not any("docker" in s["function"]["name"] for s in driver._tool_specs())


def test_docker_tools_present_with_docker(monkeypatch):
    # 注入 DockerClient → 三件套在 handlers 和 specs 里都出现
    settings = _settings(monkeypatch, LLM_BASE_URL="http://llm.test/v1", LLM_MODEL="m")
    driver = LLMDriver(httpx.AsyncClient(), settings, DockerClient("unix:///tmp/x.sock"))
    handlers = driver._handlers()
    assert {"docker_ps", "docker_logs", "docker_inspect"} <= set(handlers)
    spec_names = {s["function"]["name"] for s in driver._tool_specs()}
    assert {"docker_ps", "docker_logs", "docker_inspect"} <= spec_names


async def test_docker_inspect_delegates_to_inspect_safe(monkeypatch):
    # _docker_inspect 委托给 DockerClient.inspect_safe:白名单遮蔽密钥(Env 仅留变量名)。
    # respx 在 httpx 传输层拦截,UDS socket 不需真实存在(同既有 docker inspect 测试套路)。
    settings = _settings(monkeypatch, LLM_BASE_URL="http://llm.test/v1", LLM_MODEL="m")
    payload = {
        "Name": "/api",
        "Config": {"Image": "api:1", "Env": ["DB_PASSWORD=hunter2", "MODE=prod"]},
        "HostConfig": {"Binds": ["/etc:/etc"], "RestartPolicy": {"Name": "always"}},
        "State": {"Status": "running", "Running": True},
        "RestartCount": 0,
    }
    driver = LLMDriver(httpx.AsyncClient(), settings, DockerClient("unix:///tmp/x.sock"))
    with respx.mock:
        respx.get("http://docker/containers/api/json").mock(
            return_value=httpx.Response(200, json=payload)
        )
        out = await driver._docker_inspect("api")
    data = json.loads(out)
    # 白名单只保留这些键;Binds/HostConfig/Mounts 等绝不出现
    assert data["Name"] == "/api"
    assert data["Image"] == "api:1"
    assert data["RestartPolicy"] == "always"
    assert data["Env"] == ["DB_PASSWORD", "MODE"]  # 变量名,值已丢弃
    assert "hunter2" not in out and "/etc:/etc" not in out
    assert "HostConfig" not in data and "Binds" not in out


def test_parse_diagnosis_fence_and_fallback():
    assert parse_diagnosis(FINAL_TEXT) == DIAG_JSON
    # 无围栏退化到首个 {...}
    assert parse_diagnosis('前缀 {"summary": "s"} 后缀') == {"summary": "s"}
    # 缺必须字段/非 json → None
    assert parse_diagnosis('{"foo": 1}') is None
    assert parse_diagnosis("没有结论") is None
