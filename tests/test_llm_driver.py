# tests/test_llm_driver.py
import json

import httpx
import respx

from sentinel.docker_client import DockerClient
from sentinel.findings import EventRecord
from sentinel.llm_config import LLMProfile
from sentinel.llm_driver import LLMDriver, parse_diagnosis

PROFILE = LLMProfile(name="t", base_url="http://llm.test/v1", api_key="sk-test-key", model="m")

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


async def test_diagnose_runs_tool_loop(monkeypatch):
    settings = _settings(
        monkeypatch,
        LLM_BASE_URL="http://llm.test/v1",
        LLM_MODEL="m",
        LLM_API_KEY="sk-test-key",
        SENTINEL_PROMETHEUS_URL="http://prometheus:9090",
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
            diagnosis, raw, _ = await driver.diagnose(_event(), profile=PROFILE)

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
    settings = _settings(
        monkeypatch,
        LLM_BASE_URL="http://llm.test/v1",
        LLM_MODEL="m",
        SENTINEL_PROMETHEUS_URL="http://prometheus:9090",
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
            respx.get("http://prometheus:9090/api/v1/query").mock(return_value=httpx.Response(500))
            diagnosis, _, _ = await driver.diagnose(_event(), profile=PROFILE)
    assert diagnosis == DIAG_JSON
    second = json.loads(llm.calls[1].request.content)
    tool_msg = [m for m in second["messages"] if m["role"] == "tool"][0]
    assert tool_msg["content"].startswith("tool prom_query failed")


async def test_unknown_tool_and_bad_args(monkeypatch):
    settings = _settings(
        monkeypatch,
        LLM_BASE_URL="http://llm.test/v1",
        LLM_MODEL="m",
        SENTINEL_PROMETHEUS_URL="http://prometheus:9090",
    )
    async with httpx.AsyncClient() as client:
        driver = LLMDriver(client, settings)
        out, ok = await driver._run_tool("no_such", {})
        assert out.startswith("unknown tool") and ok is False
        out, ok = await driver._run_tool("prom_query", {"q": "up"})  # 参数名不对 → TypeError 被吞
        assert out.startswith("tool prom_query failed") and ok is False


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
            diagnosis, _, _ = await driver.diagnose(_event(), profile=PROFILE)
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
            text = await driver.summarize({"date": "2026-06-12", "services": []}, profile=PROFILE)
    assert text == "整体平稳,无异常。"
    body = json.loads(llm.calls[0].request.content)
    assert "tools" not in body  # 总结数据全内联,不需要工具
    assert "2026-06-12" in body["messages"][0]["content"]


async def test_loki_logs_formats_lines(monkeypatch):
    settings = _settings(
        monkeypatch,
        LLM_BASE_URL="http://llm.test/v1",
        LLM_MODEL="m",
        SENTINEL_LOKI_URL="http://loki:3100",
    )
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


async def test_diagnose_captures_tool_calls(monkeypatch):
    settings = _settings(
        monkeypatch,
        LLM_BASE_URL="http://llm.test/v1",
        LLM_MODEL="m",
        SENTINEL_PROMETHEUS_URL="http://prometheus:9090",
    )
    async with httpx.AsyncClient() as client:
        driver = LLMDriver(client, settings)
        with respx.mock:
            respx.post(LLM_URL).mock(
                side_effect=[
                    _llm_message(
                        tool_calls=[
                            {
                                "id": "c1",
                                "type": "function",
                                "function": {"name": "prom_query", "arguments": '{"query": "up"}'},
                            }
                        ]
                    ),
                    _llm_message(content=FINAL_TEXT),
                ]
            )
            respx.get("http://prometheus:9090/api/v1/query").mock(
                return_value=httpx.Response(200, json={"status": "success", "data": {}})
            )
            diagnosis, raw, tool_calls = await driver.diagnose(_event(), profile=PROFILE)
    assert diagnosis == DIAG_JSON
    assert "```json" in raw
    assert len(tool_calls) == 1
    tc = tool_calls[0]
    assert tc["tool"] == "prom_query"
    assert tc["args"] == {"query": "up"}
    assert tc["ok"] is True
    assert "success" in tc["output"]


async def test_evidence_capped_but_model_gets_full_output(monkeypatch):
    """证据链落库/展示截断到 4096,但喂给模型的工具输出保持完整(≤8000),决策质量不受影响。
    这是子项目③最易被未来重构破坏的不变量:截断只作用于持久化/展示副本,不作用于模型上下文。"""
    settings = _settings(
        monkeypatch,
        LLM_BASE_URL="http://llm.test/v1",
        LLM_MODEL="m",
        SENTINEL_PROMETHEUS_URL="http://prometheus:9090",
    )
    big = "x" * 6000  # >4096(面板截断)且 <8000(_MAX_TOOL_OUT,不被它截)
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
                                "function": {"name": "prom_query", "arguments": '{"query": "up"}'},
                            }
                        ]
                    ),
                    _llm_message(content=FINAL_TEXT),
                ]
            )
            respx.get("http://prometheus:9090/api/v1/query").mock(
                return_value=httpx.Response(200, text=big)
            )
            _, _, tool_calls = await driver.diagnose(_event(), profile=PROFILE)
    # 模型在第二轮收到完整 6000 字符(未被面板的 4096 截断)
    second = json.loads(llm.calls[1].request.content)
    tool_msg = next(m for m in second["messages"] if m["role"] == "tool")
    assert len(tool_msg["content"]) == 6000
    assert "(truncated)" not in tool_msg["content"]
    # 但落库/展示的证据链被截断到 4096 + 截断标记
    out = tool_calls[0]["output"]
    assert out.endswith("…(truncated)")
    assert len(out) == 4096 + len("…(truncated)")
    assert out[:4096] == big[:4096]


async def test_diagnose_captures_failed_tool_call(monkeypatch):
    settings = _settings(
        monkeypatch,
        LLM_BASE_URL="http://llm.test/v1",
        LLM_MODEL="m",
        SENTINEL_PROMETHEUS_URL="http://prometheus:9090",
    )
    async with httpx.AsyncClient() as client:
        driver = LLMDriver(client, settings)
        with respx.mock:
            respx.post(LLM_URL).mock(
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
            _, _, tool_calls = await driver.diagnose(_event(), profile=PROFILE)
    assert tool_calls[0]["ok"] is False
    assert tool_calls[0]["output"].startswith("tool prom_query failed")


def test_truncate_caps_output():
    from sentinel.llm_driver import PANEL_TOOL_OUTPUT_CAP, _truncate

    assert _truncate("abc", 10) == "abc"
    long = "x" * (PANEL_TOOL_OUTPUT_CAP + 50)
    out = _truncate(long, PANEL_TOOL_OUTPUT_CAP)
    assert out.endswith("…(truncated)")
    assert len(out) == PANEL_TOOL_OUTPUT_CAP + len("…(truncated)")


def test_diag_system_selects_by_lang():
    from sentinel.llm_driver import _DIAG_SYSTEM, _DIAG_SYSTEM_EN, _diag_system

    assert _diag_system("en") is _DIAG_SYSTEM_EN
    assert _diag_system("zh") is _DIAG_SYSTEM
    assert _diag_system("anything-else") is _DIAG_SYSTEM  # 默认 zh
    # 两版都强制最终输出一个 json 代码块
    assert "```json" in _DIAG_SYSTEM and "```json" in _DIAG_SYSTEM_EN
    assert "只读" in _DIAG_SYSTEM and "read-only" in _DIAG_SYSTEM_EN


async def test_diagnose_uses_english_system_prompt_when_configured(monkeypatch):
    from sentinel.llm_driver import _DIAG_SYSTEM_EN

    settings = _settings(
        monkeypatch,
        LLM_BASE_URL="http://llm.test/v1",
        LLM_MODEL="m",
        LLM_API_KEY="sk-test-key",
        SENTINEL_LLM_LANG="en",
    )
    async with httpx.AsyncClient() as client:
        driver = LLMDriver(client, settings)
        with respx.mock:
            llm = respx.post(LLM_URL).mock(side_effect=[_llm_message(content=FINAL_TEXT)])
            await driver.diagnose(_event(), profile=PROFILE)
    sent = json.loads(llm.calls[0].request.content)
    assert sent["messages"][0]["role"] == "system"
    assert sent["messages"][0]["content"] == _DIAG_SYSTEM_EN
