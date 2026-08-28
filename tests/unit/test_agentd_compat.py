import agentd.patch as agentd_patch
from openai.types.responses import ResponseFunctionCallArgumentsDoneEvent

from utils.agentd_compat import ensure_agentd_openai_event_compatibility


def test_agentd_done_event_adapter_supplies_sdk_required_name(monkeypatch):
    monkeypatch.setattr(
        agentd_patch,
        "ResponseFunctionCallArgumentsDoneEvent",
        ResponseFunctionCallArgumentsDoneEvent,
    )

    assert ensure_agentd_openai_event_compatibility() is True
    event = agentd_patch.ResponseFunctionCallArgumentsDoneEvent(
        arguments='{"query":"example"}',
        item_id="call-1",
        output_index=0,
        sequence_number=2,
        type="response.function_call_arguments.done",
    )

    assert event.name == ""
    assert ensure_agentd_openai_event_compatibility() is False


def test_agentd_done_event_adapter_preserves_explicit_name(monkeypatch):
    monkeypatch.setattr(
        agentd_patch,
        "ResponseFunctionCallArgumentsDoneEvent",
        ResponseFunctionCallArgumentsDoneEvent,
    )
    ensure_agentd_openai_event_compatibility()

    event = agentd_patch.ResponseFunctionCallArgumentsDoneEvent(
        arguments="{}",
        item_id="call-2",
        name="OpenSearch Retrieval Tool",
        output_index=0,
        sequence_number=2,
        type="response.function_call_arguments.done",
    )

    assert event.name == "OpenSearch Retrieval Tool"
