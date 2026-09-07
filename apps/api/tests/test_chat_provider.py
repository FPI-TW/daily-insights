import json

import httpx
import pytest

from daily_insights_api.modules.chat.provider import OpenAICompatibleChatProvider


async def test_chat_provider_caps_output_in_actual_http_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, text='data: {"choices":[{"delta":{"content":"answer"}}]}\n\ndata: [DONE]\n\n'
        )

    original = httpx.AsyncClient

    def client(**kwargs: object) -> httpx.AsyncClient:
        assert kwargs == {"trust_env": False, "follow_redirects": False}
        return original(transport=httpx.MockTransport(handle))

    monkeypatch.setattr(httpx, "AsyncClient", client)
    provider = OpenAICompatibleChatProvider(
        base_url="https://model.example", api_key="test", max_output_tokens=128
    )
    stream = await provider.stream(
        model="test", messages=[{"role": "user", "content": "hello"}], timeout_seconds=1
    )
    assert [chunk async for chunk in stream] == ["answer"]
    assert requests[0]["max_tokens"] == 128
    assert requests[0]["messages"] == [{"role": "user", "content": "hello"}]
