
import pytest

from agent import async_response


class BrokenOutputTextResponse:
    id = "non-stream-response"
    error = None

    @property
    def output_text(self):
        raise TypeError("'NoneType' object is not iterable")


class FakeResponses:
    def __init__(self):
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return BrokenOutputTextResponse()


class FakeClient:
    default_headers: dict[str, str] = {}
    api_key = "test-key"

    def __init__(self):
        self.responses = FakeResponses()


@pytest.mark.asyncio
async def test_async_response_rejects_non_streaming_response_without_output_text():
    """Streaming fallback belongs to ``async_response_stream``, not this API."""
    client = FakeClient()

    with pytest.raises(ValueError, match="Nudge response missing output_text"):
        await async_response(
            client,
            prompt="",
            model="flow-id",
        )

    assert [call["stream"] for call in client.responses.calls] == [False]
