"""Narrow compatibility fixes for the pinned AgentD/OpenAI SDK boundary.

AgentD 0.8.7 emits a synthetic ``response.function_call_arguments.done``
event after executing a tool. OpenAI SDK 2.24 requires that event to carry a
``name`` field, while AgentD constructs it without one. The validation error
otherwise interrupts the response stream after retrieval has completed.

The adapter preserves an explicit name and otherwise uses an empty value: the
authoritative tool name is already present in the surrounding
``response.output_item`` events. The schema-aware guard becomes a no-op after
AgentD implements the current SDK contract itself.
"""

from typing import Any

import agentd.patch as agentd_patch

_COMPATIBILITY_MARKER = "__openrag_openai_224_compatibility__"


def ensure_agentd_openai_event_compatibility() -> bool:
    """Make AgentD's synthetic function-argument event valid for OpenAI 2.24."""

    event_factory = agentd_patch.ResponseFunctionCallArgumentsDoneEvent
    if getattr(event_factory, _COMPATIBILITY_MARKER, False):
        return False

    model_fields = getattr(event_factory, "model_fields", {})
    name_field = model_fields.get("name")
    if name_field is None or not name_field.is_required():
        return False

    def compatible_event_factory(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("name", "")
        return event_factory(*args, **kwargs)

    setattr(compatible_event_factory, _COMPATIBILITY_MARKER, True)
    agentd_patch.ResponseFunctionCallArgumentsDoneEvent = compatible_event_factory
    return True
