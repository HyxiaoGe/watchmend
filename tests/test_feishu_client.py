# tests/test_feishu_client.py
import base64
import hashlib
import hmac

import httpx
import pytest
import respx

from sentinel.feishu.client import FeishuClient, FeishuError, gen_sign


def test_gen_sign_matches_reference_vector():
    # 参考实现:key = f"{ts}\n{secret}",签空消息
    ts, secret = 1700000000, "abc123"
    expected = base64.b64encode(
        hmac.new(f"{ts}\n{secret}".encode(), digestmod=hashlib.sha256).digest()
    ).decode()
    assert gen_sign(secret, ts) == expected


@pytest.mark.asyncio
async def test_send_includes_timestamp_and_sign_when_secret_set():
    captured = {}

    def _capture(request):
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"code": 0, "msg": "success"})

    with respx.mock:
        respx.post("https://open.feishu.cn/hook/T").mock(side_effect=_capture)
        async with httpx.AsyncClient() as client:
            fc = FeishuClient(client, "https://open.feishu.cn/hook/T", secret="s", min_interval=0)
            await fc.send({"msg_type": "interactive", "card": {}})
    assert "timestamp" in captured and "sign" in captured
    assert captured["msg_type"] == "interactive"


@pytest.mark.asyncio
async def test_nonzero_code_raises_even_on_http_200():
    with respx.mock:
        respx.post("https://open.feishu.cn/hook/T").mock(
            return_value=httpx.Response(200, json={"code": 19021, "msg": "sign match fail"})
        )
        async with httpx.AsyncClient() as client:
            fc = FeishuClient(client, "https://open.feishu.cn/hook/T", secret="s", min_interval=0)
            with pytest.raises(FeishuError) as exc:
                await fc.send({"msg_type": "interactive", "card": {}})
    assert "19021" in str(exc.value)


@pytest.mark.asyncio
async def test_no_sign_when_secret_absent():
    captured = {}

    def _capture(request):
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"code": 0})

    with respx.mock:
        respx.post("https://open.feishu.cn/hook/T").mock(side_effect=_capture)
        async with httpx.AsyncClient() as client:
            fc = FeishuClient(client, "https://open.feishu.cn/hook/T", secret=None, min_interval=0)
            await fc.send({"msg_type": "interactive", "card": {}})
    assert "sign" not in captured and "timestamp" not in captured
