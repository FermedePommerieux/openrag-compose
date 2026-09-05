from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from langchain.agents import create_agent
from langchain.agents.middleware import (
    HumanInTheLoopMiddleware,
    ModelCallLimitMiddleware,
    ToolRetryMiddleware,
)
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from lfx.components.models_and_agents.agent_helpers.graph_event_adapter import (
    adapt_graph_events_to_executor_shape,
)
from lfx.components.models_and_agents.agent_helpers.messages_input_builder import (
    build_initial_messages,
)
from lfx.components.models_and_agents.agent_helpers.placeholder_corrective_middleware import (
    WatsonXPlaceholderMiddleware,
)
from lfx.components.models_and_agents.agent_helpers.single_tool_call_middleware import (
    SingleToolCallMiddleware,
)
from lfx.components.models_and_agents.agent_helpers.tool_approval import ToolApprovalMixin
from lfx.components.models_and_agents.agent_helpers.tool_call_id_middleware import (
    ToolCallIDMiddleware,
)
from lfx.components.models_and_agents.memory import (
    MemoryComponent,
    _safe_graph_user_id,
    aget_agent_chat_history,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain_core.tools import Tool
    from lfx.schema.log import OnTokenFunctionType, SendMessageFunctionType

from lfx.base.agents.agent import LCToolsAgentComponent
from lfx.base.agents.callback import AgentAsyncHandler
from lfx.base.agents.default_system_prompt import DEFAULT_SYSTEM_PROMPT_TEMPLATE
from lfx.base.agents.events import AgentPausedError, ExceptionWithMessageError, process_agent_events
from lfx.base.agents.token_callback import TokenUsageCallbackHandler
from lfx.base.agents.utils import get_chat_output_sender_name
from lfx.base.constants import STREAM_INFO_TEXT
from lfx.base.models.unified_models import (
    get_language_model_options,
    get_llm,
    handle_model_input_update,
)
from lfx.base.models.watsonx_constants import IBM_WATSONX_URLS
from lfx.components.agentics.helpers.model_config import validate_model_selection
from lfx.components.helpers import CalculatorComponent, CurrentDateComponent
from lfx.components.langchain_utilities.ibm_granite_handler import is_watsonx_model
from lfx.components.langchain_utilities.tool_calling import ToolCallingAgentComponent
from lfx.custom.custom_component.component import get_component_toolkit
from lfx.field_typing.range_spec import RangeSpec
from lfx.inputs.inputs import BoolInput, DropdownInput, ModelInput, StrInput
from lfx.io import IntInput, MessageTextInput, MultilineInput, Output, SecretStrInput, TableInput
from lfx.log.logger import logger
from lfx.memory import delete_message
from lfx.schema.data import Data
from lfx.schema.dotdict import dotdict
from lfx.schema.message import Message
from lfx.schema.table import EditMode
from lfx.utils.constants import MESSAGE_SENDER_AI

# BEGIN GENERATED SCOPE COVERAGE CONTRACT
SCOPE_COVERAGE_MESSAGES = {
    "complete": (
        "The accessible provenance-connected scope discovered from the ranked seeds "
        "was closed and every discovered document snapshot was read and verified."
    ),
    "incomplete_seed_discovery": "Ranked seed discovery did not complete.",
    "search_error": "Ranked seed discovery failed with a search error.",
    "retrieval_execution_incomplete": (
        "The requested retrieval profile was not executed completely."
    ),
    "multi_query_planner_failed": "The requested multi-query planner did not complete.",
    "multi_query_query_failed": "At least one planned discovery query did not complete.",
    "retrieval_lexical_lane_failed": "The required lexical retrieval lane did not complete.",
    "retrieval_dense_lane_failed": "The required dense retrieval lane did not complete.",
    "retrieval_fusion_failed": "The required retrieval fusion did not complete.",
    "no_provenance_seed": "No valid provenance-bearing seed document was discovered.",
    "seed_missing_provenance": (
        "At least one discovered seed document has missing or invalid provenance."
    ),
    "graph_limit_reached": "A provenance graph traversal limit stopped closure.",
    "graph_traversal_failed": "Provenance graph traversal failed before closure.",
    "scope_policy_unclassified_relation": (
        "At least one visible provenance relation could not be classified by the "
        "declared documentary scope policy."
    ),
    "document_limit_reached": "The document discovery limit stopped closure.",
    "document_read_incomplete": "At least one discovered document was not read completely.",
    "legacy_document": "At least one document has no verifiable ingestion profile.",
    "snapshot_changed": "At least one document snapshot changed while it was being read.",
    "cursor_invalid": "At least one document continuation cursor was invalid.",
    "access_error": "At least one discovered document could not be read in this access scope.",
    "profile_invalid": "At least one document verification profile or coverage counter is invalid.",
    "identity_ambiguous": (
        "A provenance alternate identifier resolves to more than one accessible entity."
    ),
}

SCOPE_COVERAGE_CODE_ORDER = tuple(SCOPE_COVERAGE_MESSAGES)
SCOPE_COVERAGE_CONTRACT_ID = "openrag.scope-coverage"
SCOPE_COVERAGE_CONTRACT_VERSION = 1


@dataclass(frozen=True)
class ScopeCertificationFacts:
    """Measured facts consumed by the sole scope certification decision.

    Counts and ratios describe work performed; they never certify coverage by
    themselves. Completion additionally requires valid seeds, natural graph
    closure and verified complete reads for every accessible discovered
    document.
    """

    seed_discovery_complete: bool
    seed_documents: int
    valid_provenance_seed_documents: int
    invalid_provenance_seed_documents: int
    graph_frontier_empty: bool
    graph_limit_reached: bool
    graph_stop_reason: str | None
    graph_failed: bool
    retrieval_execution_complete: bool
    documents_discovered: int
    documents_complete: int
    covered_chunks: int
    total_chunks: int
    document_failure_codes: tuple[str, ...] = ()
    seed_failure_code: str | None = None
    unclassified_relations: int = 0
    retrieval_failure_codes: tuple[str, ...] = ()


def _scope_certification_facts_payload(facts: ScopeCertificationFacts) -> dict[str, Any]:
    """Return the canonical JSON-safe inputs to the scope certifier."""
    return {
        "seed_discovery_complete": facts.seed_discovery_complete,
        "seed_documents": facts.seed_documents,
        "valid_provenance_seed_documents": facts.valid_provenance_seed_documents,
        "invalid_provenance_seed_documents": facts.invalid_provenance_seed_documents,
        "graph_frontier_empty": facts.graph_frontier_empty,
        "graph_limit_reached": facts.graph_limit_reached,
        "graph_stop_reason": facts.graph_stop_reason,
        "graph_failed": facts.graph_failed,
        "retrieval_execution_complete": facts.retrieval_execution_complete,
        "documents_discovered": facts.documents_discovered,
        "documents_complete": facts.documents_complete,
        "covered_chunks": facts.covered_chunks,
        "total_chunks": facts.total_chunks,
        "document_failure_codes": list(facts.document_failure_codes),
        "seed_failure_code": facts.seed_failure_code,
        "unclassified_relations": facts.unclassified_relations,
        "retrieval_failure_codes": list(facts.retrieval_failure_codes),
    }


def _scope_certification_facts_sha256(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def certify_scope_coverage(facts: ScopeCertificationFacts) -> dict[str, Any]:
    """Return one deterministic, fail-closed scope coverage decision."""
    failures: set[str] = set()
    if facts.seed_failure_code:
        failures.add(
            facts.seed_failure_code
            if facts.seed_failure_code in SCOPE_COVERAGE_MESSAGES
            and facts.seed_failure_code != "complete"
            else "search_error"
        )
    elif not facts.seed_discovery_complete:
        failures.add("incomplete_seed_discovery")

    recognized_retrieval_failures = {
        code
        for code in facts.retrieval_failure_codes
        if code in SCOPE_COVERAGE_MESSAGES and code != "complete"
    }
    failures.update(recognized_retrieval_failures)
    if not facts.retrieval_execution_complete and not recognized_retrieval_failures:
        failures.add("retrieval_execution_incomplete")
    if facts.retrieval_execution_complete and facts.retrieval_failure_codes:
        failures.add("profile_invalid")

    if facts.seed_discovery_complete:
        if facts.seed_documents <= 0 or facts.valid_provenance_seed_documents <= 0:
            failures.add("no_provenance_seed")
        elif (
            facts.invalid_provenance_seed_documents > 0
            or facts.valid_provenance_seed_documents != facts.seed_documents
        ):
            failures.add("seed_missing_provenance")

        if facts.graph_failed:
            failures.add("graph_traversal_failed")
        if facts.graph_limit_reached:
            if facts.graph_stop_reason == "max_documents":
                failures.add("document_limit_reached")
            elif facts.graph_stop_reason == "ambiguous_alternate_id":
                failures.add("identity_ambiguous")
            else:
                failures.add("graph_limit_reached")
        elif not facts.graph_frontier_empty:
            failures.add("graph_traversal_failed")
        elif facts.graph_stop_reason != "frontier_empty":
            # An empty frontier is a measured state, not by itself proof that
            # traversal stopped naturally.  A contradictory stop reason must
            # never be upgraded to a complete certificate.
            failures.add("profile_invalid")
        if facts.unclassified_relations > 0:
            failures.add("scope_policy_unclassified_relation")

        recognized_document_failures = {
            code
            for code in facts.document_failure_codes
            if code in SCOPE_COVERAGE_MESSAGES and code != "complete"
        }
        failures.update(recognized_document_failures)
        if facts.document_failure_codes and not recognized_document_failures:
            failures.add("document_read_incomplete")
        if (
            facts.documents_complete < facts.documents_discovered
            and not facts.document_failure_codes
        ):
            failures.add("document_read_incomplete")
        if facts.covered_chunks < facts.total_chunks and not facts.document_failure_codes:
            failures.add("document_read_incomplete")
    if (
        min(
            facts.seed_documents,
            facts.valid_provenance_seed_documents,
            facts.invalid_provenance_seed_documents,
            facts.documents_discovered,
            facts.documents_complete,
            facts.covered_chunks,
            facts.total_chunks,
            facts.unclassified_relations,
        )
        < 0
        or facts.valid_provenance_seed_documents + facts.invalid_provenance_seed_documents
        != facts.seed_documents
        or facts.documents_complete > facts.documents_discovered
        or facts.covered_chunks > facts.total_chunks
    ):
        failures.add("profile_invalid")

    ordered_failures = [code for code in SCOPE_COVERAGE_CODE_ORDER if code in failures]
    complete = not ordered_failures
    status_code = "complete" if complete else ordered_failures[0]
    fact_payload = _scope_certification_facts_payload(facts)
    return {
        "complete": complete,
        "status_code": status_code,
        "status_message": SCOPE_COVERAGE_MESSAGES[status_code],
        "failure_codes": ordered_failures,
        "certification": {
            "contract_id": SCOPE_COVERAGE_CONTRACT_ID,
            "contract_version": SCOPE_COVERAGE_CONTRACT_VERSION,
            "facts": fact_payload,
            "facts_sha256": _scope_certification_facts_sha256(fact_payload),
        },
    }


def retrieval_execution_complete(requested: dict[str, Any], effective: dict[str, Any]) -> bool:
    """Return True only when every required retrieval capability succeeded."""

    requested_lanes = requested.get("lanes", {})
    effective_lanes = effective.get("lanes", {})
    if (
        not isinstance(requested_lanes, dict)
        or not isinstance(effective_lanes, dict)
        or not requested_lanes
    ):
        return False
    for lane, requirement in requested_lanes.items():
        if requirement != "required":
            continue
        lane_status = effective_lanes.get(lane, {})
        if not isinstance(lane_status, dict) or lane_status.get("status") != "succeeded":
            return False
    return True


def verify_scope_coverage_certificate(coverage: dict[str, Any]) -> dict[str, Any]:
    """Re-run the canonical certifier over a transported scope certificate.

    Product and benchmark consumers use this verifier instead of rebuilding a
    second completion contract.  It detects missing canonical provenance,
    edited facts, edited decisions, and disagreement between public counters
    and the facts that were actually certified.
    """
    failures: list[str] = []
    certification = coverage.get("certification")
    if not isinstance(certification, dict):
        return {"valid": False, "failure_codes": ["canonical_certification_missing"]}
    if certification.get("contract_id") != SCOPE_COVERAGE_CONTRACT_ID:
        failures.append("coverage_contract_id_invalid")
    if (
        type(certification.get("contract_version")) is not int
        or certification.get("contract_version") != SCOPE_COVERAGE_CONTRACT_VERSION
    ):
        failures.append("coverage_contract_version_invalid")

    raw_facts = certification.get("facts")
    if not isinstance(raw_facts, dict):
        return {
            "valid": False,
            "failure_codes": sorted({*failures, "certification_facts_invalid"}),
        }
    expected_fact_keys = set(
        _scope_certification_facts_payload(
            ScopeCertificationFacts(
                seed_discovery_complete=False,
                seed_documents=0,
                valid_provenance_seed_documents=0,
                invalid_provenance_seed_documents=0,
                graph_frontier_empty=False,
                graph_limit_reached=False,
                graph_stop_reason=None,
                graph_failed=False,
                retrieval_execution_complete=False,
                documents_discovered=0,
                documents_complete=0,
                covered_chunks=0,
                total_chunks=0,
            )
        )
    )
    if set(raw_facts) != expected_fact_keys:
        failures.append("certification_facts_invalid")

    bool_fields = (
        "seed_discovery_complete",
        "graph_frontier_empty",
        "graph_limit_reached",
        "graph_failed",
        "retrieval_execution_complete",
    )
    int_fields = (
        "seed_documents",
        "valid_provenance_seed_documents",
        "invalid_provenance_seed_documents",
        "documents_discovered",
        "documents_complete",
        "covered_chunks",
        "total_chunks",
        "unclassified_relations",
    )
    if any(type(raw_facts.get(fact_name)) is not bool for fact_name in bool_fields):
        failures.append("certification_facts_invalid")
    if any(type(raw_facts.get(fact_name)) is not int for fact_name in int_fields):
        failures.append("certification_facts_invalid")
    if raw_facts.get("graph_stop_reason") is not None and not isinstance(
        raw_facts.get("graph_stop_reason"), str
    ):
        failures.append("certification_facts_invalid")
    if raw_facts.get("seed_failure_code") is not None and not isinstance(
        raw_facts.get("seed_failure_code"), str
    ):
        failures.append("certification_facts_invalid")
    for fact_name in ("document_failure_codes", "retrieval_failure_codes"):
        values = raw_facts.get(fact_name)
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            failures.append("certification_facts_invalid")
    if "certification_facts_invalid" in failures:
        return {"valid": False, "failure_codes": sorted(set(failures))}

    canonical_facts = dict(raw_facts)
    supplied_digest = certification.get("facts_sha256")
    expected_digest = _scope_certification_facts_sha256(canonical_facts)
    if (
        not isinstance(supplied_digest, str)
        or len(supplied_digest) != 64
        or any(c not in "0123456789abcdef" for c in supplied_digest)
        or not hmac.compare_digest(supplied_digest, expected_digest)
    ):
        failures.append("certification_facts_digest_mismatch")

    facts = ScopeCertificationFacts(
        **{
            **canonical_facts,
            "document_failure_codes": tuple(canonical_facts["document_failure_codes"]),
            "retrieval_failure_codes": tuple(canonical_facts["retrieval_failure_codes"]),
        }
    )
    expected = certify_scope_coverage(facts)
    for fact_name in ("complete", "status_code", "status_message", "failure_codes"):
        if (
            type(coverage.get(fact_name)) is not type(expected[fact_name])
            or coverage.get(fact_name) != expected[fact_name]
        ):
            failures.append(f"certified_decision_mismatch:{fact_name}")

    public_fact_fields = (
        "seed_discovery_complete",
        "seed_documents",
        "valid_provenance_seed_documents",
        "invalid_provenance_seed_documents",
        "retrieval_execution_complete",
        "documents_discovered",
        "documents_complete",
        "covered_chunks",
        "total_chunks",
    )
    for fact_name in public_fact_fields:
        if (
            type(coverage.get(fact_name)) is not type(canonical_facts[fact_name])
            or coverage.get(fact_name) != canonical_facts[fact_name]
        ):
            failures.append(f"certified_public_fact_mismatch:{fact_name}")
    public_graph_fields = {
        "graph_frontier_empty": "graph_frontier_empty",
        "graph_limit_reached": "graph_limit_reached",
        "graph_stop_reason": "graph_stop_reason",
        "graph_failed": "graph_failed",
    }
    for public_field, fact_field in public_graph_fields.items():
        if (
            type(coverage.get(public_field)) is not type(canonical_facts[fact_field])
            or coverage.get(public_field) != canonical_facts[fact_field]
        ):
            failures.append(f"certified_public_fact_mismatch:{public_field}")
    unclassified = coverage.get("relations_unclassified")
    public_unclassified = unclassified.get("total") if isinstance(unclassified, dict) else None
    if public_unclassified != canonical_facts["unclassified_relations"]:
        failures.append("certified_public_fact_mismatch:unclassified_relations")
    if coverage.get("retrieval_failure_codes") != canonical_facts["retrieval_failure_codes"]:
        failures.append("certified_public_fact_mismatch:retrieval_failure_codes")

    requested = coverage.get("requested_retrieval_profile")
    effective = coverage.get("effective_retrieval_profile")
    if requested is not None or effective is not None:
        if not isinstance(requested, dict) or not isinstance(effective, dict):
            failures.append("certified_retrieval_profile_invalid")
        elif (
            retrieval_execution_complete(requested, effective) != facts.retrieval_execution_complete
        ):
            failures.append("certified_retrieval_execution_mismatch")
    if "graph_execution_complete" in coverage and coverage["graph_execution_complete"] is not (
        not facts.graph_failed
    ):
        failures.append("certified_graph_execution_mismatch")
    if (
        "documents_incomplete" in coverage
        and coverage["documents_incomplete"]
        != facts.documents_discovered - facts.documents_complete
    ):
        failures.append("certified_document_count_mismatch")

    return {"valid": not failures, "failure_codes": sorted(set(failures))}
# END GENERATED SCOPE COVERAGE CONTRACT


_RETRIEVAL_TOOL_NAME = "search_documents"
_RETRIEVAL_METADATA_TOOL_NAME = "document_search_with_metadata"
_RETRIEVAL_TOOL_NAMES = frozenset({_RETRIEVAL_TOOL_NAME, _RETRIEVAL_METADATA_TOOL_NAME})
_RETRIEVAL_GUARD_METADATA_KEY = "openrag_retrieval_guard"
_RETRIEVAL_GUARD_RESULT_KEY = "openrag_retrieval_guard"
_RETRIEVAL_GUARD_VERSION = 1
_RETRIEVAL_SCOPE_POLICY_ID = "documentary-prov-o"
_RETRIEVAL_SCOPE_POLICY_VERSION = 1
_RETRIEVAL_COVERAGE_CONTRACT_ID = "openrag.scope-coverage"
_RETRIEVAL_COVERAGE_CONTRACT_VERSION = 1
_RETRIEVAL_COVERAGE_FIELDS = (
    "mode",
    "complete",
    "next_cursor",
    "seed_discovery_complete",
    "seed_documents",
    "valid_provenance_seed_documents",
    "invalid_provenance_seed_documents",
    "seed_provenance_complete",
    "scope_policy_id",
    "scope_policy_version",
    "graph_frontier_empty",
    "graph_limit_reached",
    "graph_stop_reason",
    "graph_failed",
    "graph_execution_complete",
    "graph_stability_verified",
    "documents_discovered",
    "documents_complete",
    "documents_incomplete",
    "covered_chunks",
    "total_chunks",
    "requested_retrieval_profile",
    "effective_retrieval_profile",
    "retrieval_execution_complete",
    "retrieval_failure_codes",
    "stop_reason",
    "status_code",
    "status_message",
    "relations_unclassified",
    "failure_codes",
    "certification",
)
_RETRIEVAL_INTENT_STOP_WORDS = frozenset(
    {
        "a",
        "all",
        "au",
        "aux",
        "avec",
        "d",
        "de",
        "des",
        "du",
        "et",
        "every",
        "l",
        "la",
        "le",
        "les",
        "leur",
        "leurs",
        "lie",
        "liee",
        "liees",
        "lies",
        "n",
        "no",
        "numero",
        "of",
        "pour",
        "sur",
        "the",
        "to",
        "tous",
        "toutes",
    }
)


def _canonical_hash(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _normalize_retrieval_intent(query: Any) -> str:
    """Return a stable, domain-agnostic intent hash without retaining user text."""
    text = unicodedata.normalize("NFKD", str(query or "").lower())
    text = "".join(character for character in text if not unicodedata.combining(character))
    tokens: list[str] = []
    for token in re.findall(r"[a-z0-9]+", text):
        if token in _RETRIEVAL_INTENT_STOP_WORDS:
            continue
        # A deliberately small plural fold improves order/number stability
        # without embedding business vocabulary or guessing synonyms.
        if token.isalpha() and len(token) > 4 and token.endswith("s"):
            token = token[:-1]
        tokens.append(token)
    # Stable identifiers are stronger intent anchors than surrounding wording.
    # Restrict this fold to long/mixed identifiers so ordinary years do not
    # collapse unrelated investigations. Evidence progress remains the final
    # arbiter, so different facts for one identifier can still add new chunks.
    identifiers = [
        token
        for token in tokens
        if any(character.isdigit() for character in token)
        and (len(token) >= 6 or any(character.isalpha() for character in token))
    ]
    normalized = identifiers or tokens
    return _canonical_hash({"tokens": sorted(set(normalized))})


def _message_value(message: Any, name: str, default: Any = None) -> Any:
    if isinstance(message, dict):
        return message.get(name, default)
    return getattr(message, name, default)


def _message_tool_calls(message: Any) -> list[dict[str, Any]]:
    calls = _message_value(message, "tool_calls", [])
    return [call for call in calls if isinstance(call, dict)] if isinstance(calls, list) else []


def _is_current_run_user_message(message: Any) -> bool:
    role = str(_message_value(message, "role", "") or "").lower()
    message_type = str(_message_value(message, "type", "") or "").lower()
    return role == "user" or message_type in {"human", "user"}


def _current_run_messages(messages: list[Any]) -> list[Any]:
    last_user_index = 0
    for index, message in enumerate(messages):
        if _is_current_run_user_message(message):
            last_user_index = index
    return messages[last_user_index:]


def _retrieval_mode(args: dict[str, Any]) -> str:
    if str(args.get("read_document_id") or "").strip():
        return "exhaustive"
    return "scope_exhaustive"


def _tool_name(tool: Any) -> str:
    if isinstance(tool, dict):
        function = tool.get("function")
        if isinstance(function, dict) and function.get("name"):
            return str(function["name"])
        return str(tool.get("name") or "")
    return str(getattr(tool, "name", "") or "")


def _retrieval_guard_context(tool: Any) -> dict[str, Any]:
    metadata = tool.get("metadata", {}) if isinstance(tool, dict) else getattr(tool, "metadata", {})
    metadata = metadata if isinstance(metadata, dict) else {}
    context = metadata.get(_RETRIEVAL_GUARD_METADATA_KEY, {})
    context = context if isinstance(context, dict) else {}
    return {
        "filter_fingerprint": str(context.get("filter_fingerprint") or "default"),
        "scope_policy_id": str(
            context.get("scope_policy_id") or _RETRIEVAL_SCOPE_POLICY_ID
        ),
        "scope_policy_version": context.get(
            "scope_policy_version", _RETRIEVAL_SCOPE_POLICY_VERSION
        ),
    }


def _find_retrieval_guard_context(tools: list[Any]) -> dict[str, Any]:
    for tool in tools:
        if _tool_name(tool) in _RETRIEVAL_TOOL_NAMES:
            return _retrieval_guard_context(tool)
    return _retrieval_guard_context({})


def _parse_tool_payload(content: Any) -> dict[str, Any]:
    if isinstance(content, dict):
        return content
    if isinstance(content, list):
        text_parts = [
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        content = "".join(text_parts)
    if not isinstance(content, str):
        return {}
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _evidence_identities(payload: dict[str, Any]) -> frozenset[str]:
    identities: set[str] = set()
    for payload_field, prefix in (("results", "evidence"), ("documents", "document")):
        items = payload.get(payload_field, [])
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            identity = {
                key: item.get(key)
                for key in ("document_id", "source_entity_id", "occurrence_id", "chunk_id")
                if item.get(key) not in (None, "")
            }
            if identity:
                identities.add(f"{prefix}:{_canonical_hash(identity)}")
    return frozenset(identities)


def _coverage_state(payload: dict[str, Any]) -> dict[str, Any]:
    coverage = payload.get("coverage", {})
    if not isinstance(coverage, dict):
        return {}
    state = {
        field: coverage[field]
        for field in _RETRIEVAL_COVERAGE_FIELDS
        if field in coverage
    }
    return state


def _uses_canonical_coverage_certificate(coverage: dict[str, Any]) -> bool:
    """Delegate to the exact canonical transport contract embedded above."""
    return verify_scope_coverage_certificate(coverage)["valid"]


def _call_args(call: dict[str, Any]) -> dict[str, Any]:
    args = call.get("args", {})
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}
    return args if isinstance(args, dict) else {}


@dataclass
class _RetrievalRecord:
    call_id: str
    wave: int
    normalized_intent: str
    mode: str
    exact_call_fingerprint: str
    effective_scope_fingerprint: str
    terminal_scope_fingerprint: str
    result_fingerprint: str
    retrieval_fingerprint: str
    evidence: frozenset[str]
    coverage: dict[str, Any]
    guard_reason: str | None = None
    progress: bool = False


@dataclass
class _RetrievalGuardSnapshot:
    records: list[_RetrievalRecord] = field(default_factory=list)
    terminal_scopes: set[str] = field(default_factory=set)
    exact_calls: set[str] = field(default_factory=set)
    exhaustive_scope_satisfied: bool = False
    stalled: bool = False
    latest_wave: int | None = None
    latest_wave_progress: bool | None = None
    latest_result_fingerprint: str = ""
    latest_normalized_intent: str = ""
    guard_reason: str | None = None


def _retrieval_call_keys(
    call: dict[str, Any], context: dict[str, Any]
) -> tuple[str, str, str, str, str]:
    args = _call_args(call)
    tool_name = str(call.get("name") or _RETRIEVAL_TOOL_NAME)
    query = args.get("search_query", args.get("free_text"))
    normalized_intent = _normalize_retrieval_intent(query)
    mode = _retrieval_mode(args)
    policy = {
        "scope_policy_id": context["scope_policy_id"],
        "scope_policy_version": context["scope_policy_version"],
    }
    effective_scope = {
        "tool": tool_name,
        "mode": mode,
        "filter_fingerprint": context["filter_fingerprint"],
        "document_id": str(args.get("read_document_id") or ""),
        **policy,
    }
    exact_call = {
        **effective_scope,
        "query": " ".join(str(query or "").lower().split()),
        "cursor": str(args.get("cursor") or ""),
    }
    terminal_scope = {
        "tool": tool_name,
        "normalized_intent": normalized_intent,
        "filter_fingerprint": context["filter_fingerprint"],
        **policy,
    }
    return (
        normalized_intent,
        mode,
        _canonical_hash(exact_call),
        _canonical_hash(effective_scope),
        _canonical_hash(terminal_scope),
    )


def _build_retrieval_guard_snapshot(
    messages: list[Any], context: dict[str, Any]
) -> _RetrievalGuardSnapshot:
    snapshot = _RetrievalGuardSnapshot()
    pending: dict[str, tuple[dict[str, Any], int]] = {}
    wave = 0
    for message in _current_run_messages(messages):
        retrieval_calls = [
            call
            for call in _message_tool_calls(message)
            if str(call.get("name") or "") in _RETRIEVAL_TOOL_NAMES
        ]
        if retrieval_calls:
            wave += 1
            for call in retrieval_calls:
                call_id = str(call.get("id") or "")
                if call_id:
                    pending[call_id] = (call, wave)
            continue

        call_id = str(_message_value(message, "tool_call_id", "") or "")
        pending_call = pending.get(call_id)
        if pending_call is None:
            continue
        call, call_wave = pending_call
        payload = _parse_tool_payload(_message_value(message, "content", ""))
        guard = payload.get(_RETRIEVAL_GUARD_RESULT_KEY, {})
        guard = guard if isinstance(guard, dict) else {}
        coverage = _coverage_state(payload)
        evidence = _evidence_identities(payload)
        normalized_intent, mode, exact_call, effective_scope, terminal_scope = (
            _retrieval_call_keys(call, context)
        )
        result_fingerprint = _canonical_hash(
            {"evidence": sorted(evidence), "coverage": coverage}
        )
        retrieval_fingerprint = _canonical_hash(
            {
                "tool": str(call.get("name") or _RETRIEVAL_TOOL_NAME),
                "mode": mode,
                "normalized_intent": normalized_intent,
                "filter_fingerprint": context["filter_fingerprint"],
                "scope_policy_id": context["scope_policy_id"],
                "scope_policy_version": context["scope_policy_version"],
                "effective_scope_fingerprint": effective_scope,
                "result_fingerprint": result_fingerprint,
            }
        )
        snapshot.records.append(
            _RetrievalRecord(
                call_id=call_id,
                wave=call_wave,
                normalized_intent=normalized_intent,
                mode=mode,
                exact_call_fingerprint=exact_call,
                effective_scope_fingerprint=effective_scope,
                terminal_scope_fingerprint=terminal_scope,
                result_fingerprint=result_fingerprint,
                retrieval_fingerprint=retrieval_fingerprint,
                evidence=evidence,
                coverage=coverage,
                guard_reason=str(guard.get("reason") or "") or None,
            )
        )

    seen_evidence: dict[str, set[str]] = {}
    seen_coverage: dict[str, set[str]] = {}
    wave_progress: dict[int, bool] = {}
    wave_guard: dict[int, str] = {}
    for record in snapshot.records:
        scope_evidence = seen_evidence.setdefault(record.effective_scope_fingerprint, set())
        scope_coverage = seen_coverage.setdefault(record.effective_scope_fingerprint, set())
        coverage_fingerprint = _canonical_hash(record.coverage)
        first_scope_result = not scope_evidence and not scope_coverage
        new_evidence = set(record.evidence) - scope_evidence
        coverage_changed = bool(scope_coverage) and coverage_fingerprint not in scope_coverage
        record.progress = bool(first_scope_result or new_evidence or coverage_changed)
        wave_progress[record.wave] = wave_progress.get(record.wave, False) or record.progress
        scope_evidence.update(record.evidence)
        scope_coverage.add(coverage_fingerprint)
        snapshot.exact_calls.add(record.exact_call_fingerprint)
        if record.guard_reason:
            wave_guard[record.wave] = record.guard_reason
        if (
            record.mode == "scope_exhaustive"
            and record.coverage.get("complete") is True
            and record.coverage.get("status_code") == "complete"
            and record.coverage.get("retrieval_execution_complete") is True
            and _uses_canonical_coverage_certificate(record.coverage)
        ):
            snapshot.exhaustive_scope_satisfied = True
            snapshot.terminal_scopes.add(record.terminal_scope_fingerprint)

    if snapshot.records:
        latest = snapshot.records[-1]
        snapshot.latest_wave = latest.wave
        snapshot.latest_wave_progress = wave_progress.get(latest.wave, False)
        snapshot.latest_result_fingerprint = latest.result_fingerprint
        snapshot.latest_normalized_intent = latest.normalized_intent
        if latest.wave in wave_guard:
            snapshot.stalled = True
            snapshot.guard_reason = wave_guard[latest.wave]
        elif snapshot.latest_wave_progress is False:
            snapshot.stalled = True
            snapshot.guard_reason = "retrieval_no_progress"
    return snapshot


def _retrieval_guard_reason(
    snapshot: _RetrievalGuardSnapshot,
    call: dict[str, Any],
    context: dict[str, Any],
) -> str | None:
    _intent, mode, exact_call, _effective_scope, terminal_scope = _retrieval_call_keys(
        call, context
    )
    if snapshot.stalled:
        return snapshot.guard_reason or "retrieval_no_progress"
    if mode == "scope_exhaustive" and terminal_scope in snapshot.terminal_scopes:
        return "scope_already_complete"
    if exact_call in snapshot.exact_calls:
        return "duplicate_evidence"
    return None


def _blocked_retrieval_message(
    call: dict[str, Any], reason: str, normalized_intent: str
) -> ToolMessage:
    content = {
        _RETRIEVAL_GUARD_RESULT_KEY: {
            "version": _RETRIEVAL_GUARD_VERSION,
            "reason": reason,
            "retrieval_phase": "stalled",
            "normalized_intent": normalized_intent,
            "message": (
                "Document retrieval is closed for this investigation because it would add "
                "no new evidence. Do not call search_documents again. Use the available "
                "evidence, call non-retrieval tools such as the calculator if useful, and "
                "answer with the existing coverage limitations preserved."
            ),
        },
        "results": [],
    }
    return ToolMessage(
        content=json.dumps(content, ensure_ascii=False),
        tool_call_id=str(call.get("id") or ""),
        name=str(call.get("name") or _RETRIEVAL_TOOL_NAME),
        status="success",
    )


def _compute_agent_recursion_budget(max_iterations: Any, graph_node_names: set[str]) -> int:
    """Derive the safety budget from max_iterations and compiled middleware nodes."""
    run_limit = max(1, int(max_iterations)) if max_iterations is not None else 15
    before_model = sum(name.endswith(".before_model") for name in graph_node_names)
    after_model = sum(name.endswith(".after_model") for name in graph_node_names)
    one_shot_hooks = sum(
        name.endswith((".before_agent", ".after_agent")) for name in graph_node_names
    )
    steps_per_iteration = 2 + before_model + after_model
    terminal_overhead = 2 + one_shot_hooks
    return run_limit * steps_per_iteration + terminal_overhead


class OpenRAGRetrievalGuardMiddleware(AgentMiddleware):
    """Close exhausted retrieval phases and disable search after no progress.

    State is reconstructed from ToolMessages, making the guard deterministic,
    per-run, checkpoint-safe and safe for parallel tool batches. Wrappers add no
    graph node, preserving the audited four-step loop topology.
    """

    @staticmethod
    def _messages_from_request(request: Any) -> list[Any]:
        state = getattr(request, "state", {})
        messages = _message_value(state, "messages", [])
        return messages if isinstance(messages, list) else []

    @staticmethod
    def _guard_model_request(request: Any) -> Any:
        tools = list(getattr(request, "tools", []) or [])
        context = _find_retrieval_guard_context(tools)
        snapshot = _build_retrieval_guard_snapshot(
            OpenRAGRetrievalGuardMiddleware._messages_from_request(request), context
        )
        if not snapshot.stalled:
            return request
        filtered_tools = [tool for tool in tools if _tool_name(tool) not in _RETRIEVAL_TOOL_NAMES]
        logger.info(
            "retrieval.guard "
            f"retrieval_phase=stalled normalized_intent={snapshot.latest_normalized_intent} "
            f"result_fingerprint={snapshot.latest_result_fingerprint} progress=false "
            f"guard_reason={snapshot.guard_reason}"
        )
        return request.override(tools=filtered_tools, tool_choice=None)

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        return handler(self._guard_model_request(request))

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        return await handler(self._guard_model_request(request))

    @staticmethod
    def _guard_tool_call(
        request: Any,
    ) -> tuple[_RetrievalGuardSnapshot, dict[str, Any], str | None]:
        context = _retrieval_guard_context(getattr(request, "tool", None))
        snapshot = _build_retrieval_guard_snapshot(
            OpenRAGRetrievalGuardMiddleware._messages_from_request(request), context
        )
        reason = _retrieval_guard_reason(snapshot, request.tool_call, context)
        if reason:
            normalized_intent = _retrieval_call_keys(request.tool_call, context)[0]
            logger.info(
                "retrieval.guard "
                f"retrieval_phase=stalled normalized_intent={normalized_intent} "
                f"result_fingerprint={snapshot.latest_result_fingerprint} progress=false "
                f"guard_reason={reason}"
            )
        return snapshot, context, reason

    @staticmethod
    def _log_result(request: Any, result: Any, context: dict[str, Any]) -> None:
        if not isinstance(result, ToolMessage):
            return
        messages = [*OpenRAGRetrievalGuardMiddleware._messages_from_request(request), result]
        snapshot = _build_retrieval_guard_snapshot(messages, context)
        if not snapshot.records:
            return
        latest = snapshot.records[-1]
        phase = "scope_closed" if snapshot.exhaustive_scope_satisfied else "evidence"
        logger.info(
            "retrieval.guard "
            f"retrieval_phase={phase} normalized_intent={latest.normalized_intent} "
            f"result_fingerprint={latest.result_fingerprint} "
            f"progress={str(latest.progress).lower()} guard_reason=none"
        )

    def wrap_tool_call(self, request: Any, handler: Any) -> Any:
        if str(request.tool_call.get("name") or "") not in _RETRIEVAL_TOOL_NAMES:
            return handler(request)
        _snapshot, context, reason = self._guard_tool_call(request)
        if reason:
            normalized_intent = _retrieval_call_keys(request.tool_call, context)[0]
            return _blocked_retrieval_message(request.tool_call, reason, normalized_intent)
        result = handler(request)
        self._log_result(request, result, context)
        return result

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        if str(request.tool_call.get("name") or "") not in _RETRIEVAL_TOOL_NAMES:
            return await handler(request)
        _snapshot, context, reason = self._guard_tool_call(request)
        if reason:
            normalized_intent = _retrieval_call_keys(request.tool_call, context)[0]
            return _blocked_retrieval_message(request.tool_call, reason, normalized_intent)
        result = await handler(request)
        self._log_result(request, result, context)
        return result


def set_advanced_true(component_input):
    component_input.advanced = True
    return component_input


def _agent_base_inputs():
    """Return base inputs tailored to AgentComponent's create_agent path.

    `get_base_inputs()` returns a shared list — replace, don't mutate. We drop
    inputs that are no-ops here and override info text on the inputs whose
    semantics shifted under create_agent.

    `verbose` is dropped because the create_agent event stream already surfaces
    every agent step via the "Agent Steps" content blocks; the legacy boolean
    has nothing to toggle. Saved flows that still carry a `verbose` value just
    ignore it on load (the schema no longer declares it).
    """
    drop = {"verbose"}
    overrides = {
        "handle_parsing_errors": BoolInput(
            name="handle_parsing_errors",
            display_name="Handle Parse Errors",
            value=True,
            advanced=True,
            info=(
                "Adds tool-execution retry as a safety net. `create_agent` already "
                "feeds tool-call validation errors back to the LLM automatically; "
                "this flag layers `ToolRetryMiddleware` on top so transient tool "
                "runtime failures are retried (max 2 retries)."
            ),
        ),
        "max_iterations": IntInput(
            name="max_iterations",
            display_name="Max Iterations",
            value=15,
            advanced=True,
            range_spec=RangeSpec(min=1, max=128000, step=1, step_type="int"),
            info=(
                "Maximum number of model calls the agent can make before stopping "
                "(maps to `ModelCallLimitMiddleware.run_limit` on the create_agent "
                "path). Must be at least 1 — it is a safety cap, never 'unlimited'."
            ),
        ),
    }
    return [overrides.get(inp.name, inp) for inp in LCToolsAgentComponent.get_base_inputs() if inp.name not in drop]


def _extract_text_content(value) -> str:
    """Pull a string payload from a Message-like, AIMessage-like, or string value."""
    if isinstance(value, str):
        return value
    text = getattr(value, "text", None)
    if isinstance(text, str):
        return text
    content = getattr(value, "content", None)
    if isinstance(content, str):
        return content
    return str(value) if value is not None else ""


@contextmanager
def _suppress_send_message(component: Any):
    """Temporarily replace component.send_message with a no-op for the duration of the block.

    Used during the structured-output prompt fallback: run_agent streams the agent's
    final answer through self.send_message (correct for message_response), but in
    json_response the orchestrator parses that text into structured Data which the
    downstream Chat Output emits — leaving the original emission in place produces a
    duplicate message in the playground. The original method is always restored on exit,
    even when the wrapped call raises.
    """
    original = component.send_message

    async def _noop(message, *_args, **_kwargs):
        return message

    component.send_message = _noop
    try:
        yield
    finally:
        component.send_message = original


class AgentComponent(ToolApprovalMixin, ToolCallingAgentComponent):
    display_name: str = "Agent"
    description: str = "Define the agent's instructions, then enter a task to complete using tools."
    documentation: str = "https://docs.langflow.org/agents"
    icon = "bot"
    beta = False
    name = "Agent"

    memory_inputs = [set_advanced_true(component_input) for component_input in MemoryComponent().inputs]

    inputs = [
        ModelInput(
            name="model",
            display_name="Language Model",
            info="Select your model provider",
            real_time_refresh=True,
            required=True,
            # Agents require tool calling — the filter is honored by
            # ``handle_model_input_update`` so models that can't run with
            # tools never reach the picker (and any saved selection that
            # no longer satisfies the constraint is auto-replaced).
            filters={"tool_calling": True},
        ),
        SecretStrInput(
            name="api_key",
            display_name="API Key",
            info="Overrides global provider settings. Leave blank to use your pre-configured API Key.",
            real_time_refresh=True,
            advanced=True,
        ),
        DropdownInput(
            name="base_url_ibm_watsonx",
            display_name="watsonx API Endpoint",
            info="The base URL of the API (IBM watsonx.ai only)",
            options=IBM_WATSONX_URLS,
            value=IBM_WATSONX_URLS[0],
            combobox=True,
            show=False,
            real_time_refresh=True,
        ),
        StrInput(
            name="project_id",
            display_name="watsonx Project ID",
            info="The project ID associated with the foundation model (IBM watsonx.ai only)",
            show=False,
            required=False,
        ),
        MultilineInput(
            name="system_prompt",
            display_name="Agent Instructions",
            info=(
                "System Prompt: Initial instructions and context provided to guide the agent's behavior. "
                "Supports dynamic placeholders: {current_date}, {model_name}, {optional_user_context}."
            ),
            value=DEFAULT_SYSTEM_PROMPT_TEMPLATE,
            advanced=False,
        ),
        MessageTextInput(
            name="context_id",
            display_name="Context ID",
            info="The context ID of the chat. Adds an extra layer to the local memory.",
            value="",
            advanced=True,
        ),
        IntInput(
            name="n_messages",
            display_name="Number of Chat History Messages",
            value=100,
            info="Number of chat history messages to retrieve.",
            advanced=True,
            show=True,
        ),
        IntInput(
            name="max_tokens",
            display_name="Max Tokens",
            info="Maximum number of tokens to generate. Field name varies by provider.",
            advanced=True,
            range_spec=RangeSpec(min=1, max=128000, step=1, step_type="int"),
        ),
        MultilineInput(
            name="format_instructions",
            display_name="Output Format Instructions",
            info="Generic Template for structured output formatting. Valid only with Structured response.",
            value=(
                "You are an AI that extracts structured JSON objects from unstructured text. "
                "Use a predefined schema with expected types (str, int, float, bool, dict). "
                "Extract ALL relevant instances that match the schema - if multiple patterns exist, capture them all. "
                "Fill missing or ambiguous values with defaults: null for missing values. "
                "Remove exact duplicates but keep variations that have different field values. "
                "Always return valid JSON in the expected format, never throw errors. "
                "If multiple objects can be extracted, return them all in the structured format."
            ),
            advanced=True,
        ),
        TableInput(
            name="output_schema",
            display_name="Output Schema",
            info=(
                "Schema Validation: Define the structure and data types for structured output. "
                "No validation if no output schema."
            ),
            advanced=True,
            required=False,
            value=[],
            table_schema=[
                {
                    "name": "name",
                    "display_name": "Name",
                    "type": "str",
                    "description": "Specify the name of the output field.",
                    "default": "field",
                    "edit_mode": EditMode.INLINE,
                },
                {
                    "name": "description",
                    "display_name": "Description",
                    "type": "str",
                    "description": "Describe the purpose of the output field.",
                    "default": "description of field",
                    "edit_mode": EditMode.POPOVER,
                },
                {
                    "name": "type",
                    "display_name": "Type",
                    "type": "str",
                    "edit_mode": EditMode.INLINE,
                    "description": ("Indicate the data type of the output field (e.g., str, int, float, bool, dict)."),
                    "options": ["str", "int", "float", "bool", "dict"],
                    "default": "str",
                },
                {
                    "name": "multiple",
                    "display_name": "As List",
                    "type": "boolean",
                    "description": "Set to True if this output field should be a list of the specified type.",
                    "default": "False",
                    "edit_mode": EditMode.INLINE,
                },
            ],
        ),
        *_agent_base_inputs(),
        # removed memory inputs from agent component
        # *memory_inputs,
        BoolInput(
            name="stream",
            display_name="Stream",
            info=STREAM_INFO_TEXT,
            value=True,
            advanced=True,
        ),
        BoolInput(
            name="add_current_date_tool",
            display_name="Current Date",
            advanced=True,
            info="If true, will add a tool to the agent that returns the current date.",
            value=True,
        ),
        BoolInput(
            name="add_calculator_tool",
            display_name="Calculator",
            advanced=True,
            info=(
                "If true, adds a zero-config arithmetic calculator tool to the agent "
                "(safe: only +, -, *, /, ** operators via AST)."
            ),
            value=True,
        ),
    ]
    outputs = [
        Output(name="response", display_name="Response", method="message_response"),
        Output(
            name="structured_response",
            display_name="Structured Response",
            method="json_response",
            types=["Data"],
        ),
    ]

    def _resolve_selected_model(self):
        """Resolve the selected model, including legacy agent_llm/model_name inputs."""
        try:
            from langchain_core.language_models import BaseLanguageModel

            if isinstance(self.model, BaseLanguageModel):
                return self.model
        except ImportError:
            pass

        if isinstance(self.model, list) and self.model:
            return self.model

        legacy_provider = getattr(self, "agent_llm", None)
        legacy_model_name = getattr(self, "model_name", None)
        if not legacy_provider or not legacy_model_name:
            return self.model

        options = get_language_model_options(user_id=self.user_id)
        for option in options:
            if option.get("provider") == legacy_provider and option.get("name") == legacy_model_name:
                return [option]

        return [
            {
                "name": legacy_model_name,
                "provider": legacy_provider,
                "metadata": {},
            }
        ]

    def _get_max_tokens_value(self):
        """Return the user-supplied max_tokens or None when unset/zero."""
        val = getattr(self, "max_tokens", None)
        if val in {"", 0}:
            return None
        return val

    def _model_runtime_overrides(self) -> dict[str, Any] | None:
        """Select OpenAI Responses for GPT-5.6 agents that use tools.

        Sol, Terra, and Luna reject function tools combined with reasoning on
        the legacy Chat Completions endpoint.  LangChain supports the same
        tool-and-reasoning contract through ``use_responses_api``.  Restrict
        this transport override to the real OpenAI provider so compatible
        third-party endpoints are not assumed to expose Responses.
        """
        overrides = dict(getattr(self, "_model_overrides", None) or {})
        selected = self.model
        if isinstance(selected, list) and selected and isinstance(selected[0], dict):
            provider = str(selected[0].get("provider") or "").strip().casefold()
            model_name = str(selected[0].get("name") or "").strip().casefold()
            if provider == "openai" and model_name.startswith("gpt-5.6"):
                overrides["use_responses_api"] = True
        return overrides or None

    def _get_llm(self):
        """Override parent to include max_tokens from the Agent's input field.

        Streaming is mandatory for AgentComponent: ``runnable.astream_events(v2)`` only
        emits ``on_chat_model_stream`` chunks when the underlying chat model is
        instantiated with ``streaming=True``. Unlike the LanguageModel component (where
        ``stream`` is a user-facing toggle), the Agent has no opt-out — the toggle is
        kept in the UI for backwards compatibility but is intentionally ignored here.
        Without ``stream=True``, the chat model accumulates the whole response and
        only emits ``on_chat_model_end``, silently disabling the Playground's live-
        typing view and breaking the streaming contract on the /events surface.
        """
        return get_llm(
            model=self.model,
            user_id=self.user_id,
            api_key=getattr(self, "api_key", None),
            stream=True,
            max_tokens=self._get_max_tokens_value(),
            watsonx_url=getattr(self, "base_url_ibm_watsonx", None),
            watsonx_project_id=getattr(self, "project_id", None),
            overrides=self._model_runtime_overrides(),
        )

    async def get_agent_requirements(self):
        """Get the agent requirements for the agent."""
        from langchain_core.tools import StructuredTool

        selected_model = self._resolve_selected_model()
        try:
            from langchain_core.language_models import BaseLanguageModel

            is_connected_model = isinstance(selected_model, BaseLanguageModel)
        except ImportError:
            is_connected_model = False

        if not is_connected_model:
            validate_model_selection(selected_model)

        # Ensure _get_llm() uses the resolved model (e.g. from legacy agent_llm/model_name)
        self.model = selected_model
        llm_model = self._get_llm()
        if llm_model is None:
            msg = "No language model selected. Please choose a model to proceed."
            raise ValueError(msg)

        # Get memory data
        self.chat_history = await self.get_memory_data()
        await logger.adebug(f"Retrieved {len(self.chat_history)} chat history messages")
        if isinstance(self.chat_history, Message):
            self.chat_history = [self.chat_history]

        # Add current date tool if enabled
        if self.add_current_date_tool:
            if not isinstance(self.tools, list):  # type: ignore[has-type]
                self.tools = []
            current_date_tool = (await CurrentDateComponent(**self.get_base_args()).to_toolkit()).pop(0)

            if not isinstance(current_date_tool, StructuredTool):
                msg = "CurrentDateComponent must be converted to a StructuredTool"
                raise TypeError(msg)
            # Skip if an externally-connected tool already provides the same name.
            # Duplicate tool names are rejected by Anthropic/Gemini with HTTP 400.
            if not any(getattr(t, "name", None) == current_date_tool.name for t in self.tools):
                self.tools.append(current_date_tool)

        # Add calculator tool if enabled (zero-config arithmetic)
        if getattr(self, "add_calculator_tool", False):
            if not isinstance(self.tools, list):  # type: ignore[has-type]
                self.tools = []
            calculator_tool = (await CalculatorComponent(**self.get_base_args()).to_toolkit()).pop(0)

            if not isinstance(calculator_tool, StructuredTool):
                msg = "CalculatorComponent must be converted to a StructuredTool"
                raise TypeError(msg)
            # Skip if an externally-connected tool already provides the same name.
            # Duplicate tool names are rejected by Anthropic/Gemini with HTTP 400.
            if not any(getattr(t, "name", None) == calculator_tool.name for t in self.tools):
                self.tools.append(calculator_tool)

        # Set shared callbacks for tracing the tools used by the agent
        self.set_tools_callbacks(self.tools, self._get_shared_callbacks())

        return llm_model, self.chat_history, self.tools

    def _get_resolved_model_name(self) -> str:
        """Best-effort human-readable model name for {model_name} injection."""
        try:
            from langchain_core.language_models import BaseLanguageModel

            if isinstance(self.model, BaseLanguageModel):
                return type(self.model).__name__
        except ImportError:
            pass

        if isinstance(self.model, list) and self.model:
            first = self.model[0]
            if isinstance(first, dict):
                name = first.get("name")
                if isinstance(name, str) and name:
                    return name

        legacy_model_name = getattr(self, "model_name", None)
        if isinstance(legacy_model_name, str) and legacy_model_name:
            return legacy_model_name
        return ""

    def _inject_dynamic_prompt_values(self, prompt: Any | None) -> str | None:
        """Replace known env placeholders in the system prompt.

        Handles {current_date}, {model_name}, and {optional_user_context} (the
        last one ships with the structured DEFAULT_SYSTEM_PROMPT_TEMPLATE and
        is currently unused at the AgentComponent layer, so it resolves to "").
        Uses str.replace (not str.format) so user prompts containing literal
        braces such as JSON examples ({"key": 1}) never break the agent.

        `system_prompt` is a connectable MultilineInput, so the value can arrive
        as a Message (e.g. a Prompt node wired in). Normalize it to text first —
        a raw Message has no `.replace` and used to crash the agent build.
        """
        if prompt is None:
            return None
        prompt = _extract_text_content(prompt)
        if not prompt:
            return prompt
        replacements = {
            "{current_date}": datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "{model_name}": self._get_resolved_model_name(),
            "{optional_user_context}": "",
        }
        for placeholder, value in replacements.items():
            prompt = prompt.replace(placeholder, value)
        return prompt

    def create_agent_runnable(self, *, allow_interrupts: bool = True):
        """Build the LangGraph `CompiledStateGraph` via `langchain.agents.create_agent`.

        Replaces the legacy `AgentExecutor` runnable inherited from
        `ToolCallingAgentComponent`. Other agent components (tool_calling, csv, json,
        openapi, sql*, vector_store_router) keep the legacy path — only AgentComponent
        runs on the new graph API.

        `max_iterations` and `handle_parsing_errors` (legacy AgentExecutor knobs) are
        translated to LangGraph middleware. Without that translation those user inputs
        would silently become no-ops on the new API.

        Provider notes:
        - WatsonX/Granite work natively with create_agent — `ChatWatsonx.bind_tools`
          handles tool_choice correctly. The legacy `create_granite_agent` path was
          dropped because it hardcoded `tool_choice='required'`, which the WatsonX
          API now rejects.
        - Ollama and other small/local models often emit malformed tool args. The
          ToolRetryMiddleware (default `retry_on=(Exception,)`, `on_failure='continue'`)
          catches Pydantic ValidationErrors from bad args and feeds the error back
          to the LLM as a retry signal, so the agent recovers gracefully.
        """
        llm = self._get_llm()
        tools = self.tools or []

        # Eager bind_tools validation. `create_agent(...)` is lazy — without this,
        # an LLM that doesn't support tool calling fails on the first user message
        # instead of when the user wires up the component, which is a much worse UX.
        # Gated on a non-empty tools list so a no-tool Agent on a plain chat model
        # (which legitimately has no `bind_tools`) isn't shut out at flow-build time.
        # Providers signal "no tool calling" inconsistently — `NotImplementedError`
        # (langchain default), `AttributeError` (no `bind_tools` attr), or `TypeError`
        # (signature mismatch). Treat all three as the same UX failure.
        if tools:
            try:
                llm.bind_tools(tools)
            except (NotImplementedError, AttributeError, TypeError) as exc:
                # Include the underlying error so a broken tool schema or a
                # provider implementation bug is not silently disguised as a
                # "model can't call tools" UX error.
                msg = (
                    f"{self.display_name} does not support tool calling, "
                    "or one of the connected tools failed to bind. "
                    "Please connect a tool-calling capable language model and "
                    f"verify your tools are well-formed. Underlying error: {exc!s}"
                )
                raise NotImplementedError(msg) from exc

        middleware = self._build_middleware(llm, allow_interrupts=allow_interrupts)
        checkpointer = self._build_agent_checkpointer() if allow_interrupts else None
        return create_agent(
            model=llm,
            tools=tools,
            system_prompt=self.system_prompt or "",
            middleware=middleware or None,
            checkpointer=checkpointer,
        )

    def _compute_recursion_limit(self, agent: Any | None = None) -> int:
        """Derive the safety limit from max_iterations and the compiled graph."""
        raw = getattr(self, "max_iterations", None)
        default_nodes = {
            "model",
            "tools",
            "ModelCallLimitMiddleware.before_model",
            "ModelCallLimitMiddleware.after_model",
        }
        node_names = default_nodes
        if agent is not None:
            try:
                graph_nodes = agent.get_graph().nodes
                node_names = set(graph_nodes) if graph_nodes else default_nodes
            except (AttributeError, TypeError, ValueError):
                node_names = default_nodes
        return _compute_agent_recursion_budget(raw, node_names)

    def _build_middleware(self, llm: Any, *, allow_interrupts: bool = True) -> list:
        # `llm` is passed in (rather than re-fetched via `self._get_llm()`)
        # because some providers do credential resolution / client instantiation
        # lazily on each call. The caller — `create_agent_runnable` — already
        # resolved it once for `bind_tools`, so reuse that instance here.
        middleware: list = []
        # LangChain accepts missing IDs on AIMessage.tool_calls, but LangGraph's
        # invalid-call path and ToolRetryMiddleware both require a string when
        # they construct an error ToolMessage. Normalize at the model boundary
        # so either recovery path can return the error to the model instead of
        # crashing the flow.
        if self.tools:
            middleware.append(ToolCallIDMiddleware())
            middleware.append(OpenRAGRetrievalGuardMiddleware())
        max_iterations = getattr(self, "max_iterations", None)
        if max_iterations is not None:
            # `max_iterations` is a safety cap, not an "unlimited" toggle. A saved
            # 0 or negative value (falsy) must NOT silently drop the limiter and
            # allow an unbounded model/tool loop — clamp it to a real minimum.
            run_limit = max(1, int(max_iterations))
            middleware.append(ModelCallLimitMiddleware(run_limit=run_limit))
        # ToolRetryMiddleware only matters when there ARE tools to retry. Attaching
        # it on a no-tools agent inflates the compiled graph and adds per-invocation
        # middleware overhead for nothing, which is a measurable contributor to
        # trivial-prompt latency (QA UI-003).
        if getattr(self, "handle_parsing_errors", False) and self.tools:
            middleware.append(ToolRetryMiddleware(max_retries=2))
        # WatsonX models have two known platform quirks; both still reproduce on
        # the current API, so we keep the protections from the legacy
        # `create_granite_agent` path.
        # 1. Multi-tool-call assistant turns are rejected ("This model only
        #    supports single tool-calls at once!"). Clamp to one per turn.
        # 2. Tool args occasionally come back as literal placeholder strings
        #    (e.g. `<result-from-search>`). Re-invoke once with a corrective
        #    SystemMessage.
        # Order: SingleToolCallMiddleware first (outermost) so the clamp is
        # applied to the final response, including any corrective re-invoke
        # produced by WatsonXPlaceholderMiddleware.
        if is_watsonx_model(llm):
            middleware.append(SingleToolCallMiddleware())
            middleware.append(WatsonXPlaceholderMiddleware())
        # Human-in-the-loop: attach only when a tool is gated AND interrupts are allowed
        # (the structured-output path disables them), keeping ungated flows unchanged.
        interrupt_on = self._gated_interrupt_on() if allow_interrupts else {}
        if interrupt_on:
            middleware.append(HumanInTheLoopMiddleware(interrupt_on=interrupt_on))
        return middleware

    async def run_agent(self, agent) -> Message:
        """Run the LangGraph `CompiledStateGraph` and return the final agent Message.

        Overrides the legacy `LCAgentComponent.run_agent` (which builds an
        `{"input": str, "chat_history": [...]}` dict for `AgentExecutor`). The graph
        wants `{"messages": [BaseMessage, ...]}`. The event stream is wrapped with
        `adapt_graph_events_to_executor_shape` so the legacy `process_agent_events`
        (in `lfx.base.agents.events`) can be reused unchanged.
        """
        messages = build_initial_messages(
            input_value=self.input_value,
            chat_history=getattr(self, "chat_history", None),
        )
        input_dict = {"messages": messages}

        agent_message = self._build_initial_agent_message()
        token_usage_handler = TokenUsageCallbackHandler()

        # Stream tokens to the event manager when running inside the Langflow runtime.
        # This is what powers the live-typing view in the chat UI.
        on_token_callback: OnTokenFunctionType | None = None
        if getattr(self, "_event_manager", None):
            on_token_callback = cast("OnTokenFunctionType", self._event_manager.on_token)

        # ModelCallLimit remains the semantic cap. Derive the LangGraph safety
        # net from the actual compiled middleware nodes so a valid final answer
        # cannot be discarded between after_model and END.
        recursion_limit = self._compute_recursion_limit(agent)

        agent_config: dict[str, Any] = {
            "callbacks": [
                AgentAsyncHandler(self.log),
                token_usage_handler,
                *self._get_shared_callbacks(),
            ],
            "recursion_limit": recursion_limit,
        }
        # The durable checkpointer keys on the per-run thread_id; it must be in the
        # astream_events config (not just create_agent) for both initial run and resume.
        thread_id = self._agent_thread_id()
        get_pending_interrupt = None
        stream_input: Any = input_dict
        # A checkpointer is only present when the agent was built with interrupts enabled
        # (the structured-output path builds without one); never probe its state otherwise.
        interrupts_enabled = getattr(agent, "checkpointer", None) is not None
        if interrupts_enabled and thread_id and self._gated_interrupt_on():
            agent_config["configurable"] = {"thread_id": thread_id}
            get_pending_interrupt = self._pending_interrupt_getter(agent, agent_config)
            if self._has_candidate_decision(thread_id):
                # The injected decision must match the pending interrupt's nonce, so read
                # the interrupt first; a matched decision resumes the checkpointed thread.
                value, interrupt_id = await self._read_pending_interrupt(agent, agent_config)
                decision = self._injected_agent_decision(thread_id, interrupt_id)
                if decision is not None:
                    action_requests = (value or {}).get("action_requests") or []
                    stream_input = Command(
                        resume={"decisions": self._build_resume_decisions(decision, action_requests)}
                    )
        stream = adapt_graph_events_to_executor_shape(
            agent.astream_events(stream_input, config=agent_config, version="v2")
        )
        try:
            result = await process_agent_events(
                stream,
                agent_message,
                cast("SendMessageFunctionType", self.send_message),
                on_token_callback,
                get_pending_interrupt=get_pending_interrupt,
            )
        except AgentPausedError as e:
            # Why: retract the empty partial bubble (leaks as "[]"); the HITL card supersedes it, resume re-emits it.
            paused_message = e.agent_message or agent_message
            msg_id = paused_message.get_id()
            if msg_id:
                await delete_message(id_=msg_id)
            await self._send_message_event(paused_message, category="remove_message")
            return self._suspend_for_tool_approval(e.request, paused_message)
        except ExceptionWithMessageError as e:
            # Drop the half-stored partial message from the DB (only if it was
            # actually persisted) and tell the frontend to remove the stale bubble.
            if hasattr(e, "agent_message"):
                msg_id = e.agent_message.get_id()
                if msg_id:
                    await delete_message(id_=msg_id)
                await self._send_message_event(e.agent_message, category="remove_message")
            logger.error(f"ExceptionWithMessageError: {e}")
            raise

        usage_data = token_usage_handler.get_usage()
        if usage_data:
            self._token_usage = usage_data
            result.properties.usage = usage_data
            # Only round-trip the DB when the message was stored (Chat Output wired).
            # `_should_skip_message=True` leaves `result.get_id()` empty; persisting
            # then would create a phantom row.
            if result.get_id():
                stored_result = await self._update_stored_message(result)
                await self._send_message_event(stored_result)
                result = stored_result

        self.status = result
        return result

    def _build_initial_agent_message(self) -> Message:
        """Construct the placeholder agent Message that `process_agent_events` mutates."""
        if hasattr(self, "graph"):
            session_id = self.graph.session_id
        elif hasattr(self, "_session_id"):
            session_id = self._session_id
        else:
            session_id = None

        sender_name = get_chat_output_sender_name(self) or self.display_name or "AI"
        return Message(
            sender=MESSAGE_SENDER_AI,
            sender_name=sender_name,
            properties={"icon": "Bot", "state": "partial"},
            # `text=""` sentinel so MessageTable's no_content check accepts
            # an in-flight agent message whose content_blocks haven't been
            # populated yet. Mirrors ChatInput's convention.
            text="",
            # Flat chronological event log; see lfx.base.agents.events.
            content_blocks=[],
            session_id=session_id or uuid.uuid4(),
        )

    def _selected_model_remediation_context(self) -> tuple[str | None, str | None, Any | None]:
        """Return provider/name plus a connected model target, when present."""
        try:
            selected = self._resolve_selected_model()
            if isinstance(selected, list) and selected and isinstance(selected[0], dict):
                return selected[0].get("provider"), selected[0].get("name"), None

            from langchain_core.language_models import BaseLanguageModel

            if isinstance(selected, BaseLanguageModel):
                model_name = None
                for attr in ("model_name", "model", "model_id"):
                    value = getattr(selected, attr, None)
                    if isinstance(value, str) and value:
                        model_name = value
                        break
                return self._connected_model_provider(selected), model_name, selected
        except (AttributeError, TypeError, ValueError, KeyError, ImportError):
            pass
        return None, None, None

    def _connected_model_provider(self, model: Any) -> str | None:
        """Resolve a connected model's provider from the source component.

        Runtime model classes are not provider identities: OpenAI, OpenRouter,
        and compatible endpoints can all produce ``ChatOpenAI``. The incoming
        model edge preserves the source component, so prefer its explicit
        provider override or selected-model metadata, then its provider display
        name. If the Agent is used without graph provenance, leave the provider
        unknown so provider-scoped remediations remain disabled.
        """
        vertex = getattr(self, "_vertex", None)
        if vertex is None:
            return None
        source_id = vertex.get_incoming_edge_by_target_param("model")
        if not source_id:
            return None
        source = vertex.graph.get_vertex(source_id)
        component = getattr(source, "custom_component", None)
        if component is None:
            return None

        candidate = getattr(component, "provider", None)

        if not isinstance(candidate, str) or not candidate:
            selected_model = getattr(component, "model", None)
            if isinstance(selected_model, list) and selected_model and isinstance(selected_model[0], dict):
                candidate = selected_model[0].get("provider")

        if not isinstance(candidate, str) or not candidate:
            candidate = getattr(component, "display_name", None)
        if not isinstance(candidate, str) or not candidate:
            return None

        from lfx.base.models.unified_models.provider_queries import get_model_provider_metadata

        provider_metadata = get_model_provider_metadata().get(candidate, {})
        expected_class = provider_metadata.get("mapping", {}).get("model_class")
        model_classes = {base.__name__ for base in type(model).__mro__}
        return candidate if expected_class in model_classes else None

    async def _run_agent_with_model_remediation(
        self,
        run_once: Callable[[], Awaitable[Message]],
    ) -> Message:
        """Run one Agent operation, retrying safe provider-validation failures.

        Registered remediations must identify request-validation failures that
        occur before tool execution. A failure that can happen after a tool runs
        must instead retry at the model-call boundary to avoid repeating tool
        side effects.
        """
        from lfx.base.models.model_remediation import (
            apply_overrides_to_model,
            find_remediation,
            remember,
        )

        provider, model_name, connected_model = self._selected_model_remediation_context()
        applied: set[str] = set()
        while True:
            try:
                result = await run_once()
            except (ValueError, TypeError, KeyError) as exc:
                await logger.aerror(f"{type(exc).__name__}: {exc!s}")
                raise
            except Exception as exc:
                # run_agent may wrap the provider error in ExceptionWithMessageError.
                error_text = f"{exc} {getattr(exc, '__cause__', '') or ''}"
                remediation = find_remediation(error_text, provider, already_applied=applied)
                if remediation is None:
                    await logger.aerror(f"{type(exc).__name__}: {exc!s}")
                    raise
                if connected_model is not None and not apply_overrides_to_model(connected_model, remediation.overrides):
                    await logger.aerror(
                        f"model.remediation.unapplied name={remediation.name} provider={provider} model={model_name}"
                    )
                    raise
                applied.add(remediation.name)
                if connected_model is None:
                    self._model_overrides = {
                        **(getattr(self, "_model_overrides", None) or {}),
                        **remediation.overrides,
                    }
                else:
                    # Connected outputs are shared objects across downstream flow
                    # branches. The mutation is intentionally flow-scoped: sibling
                    # branches holding this model will see the matched override too.
                    await logger.adebug(
                        f"model.remediation.shared_model name={remediation.name} provider={provider} model={model_name}"
                    )
                await logger.awarning(
                    f"model.remediation.applied name={remediation.name} provider={provider} model={model_name}"
                )
            else:
                if connected_model is None and getattr(self, "_model_overrides", None):
                    remember(provider, model_name, self._model_overrides)
                return result

    async def message_response(self) -> Message:
        async def _run_once() -> Message:
            llm_model, self.chat_history, self.tools = await self.get_agent_requirements()
            self.set(
                llm=llm_model,
                tools=self.tools or [],
                chat_history=self.chat_history,
                input_value=self.input_value,
                system_prompt=self._inject_dynamic_prompt_values(self.system_prompt),
            )
            agent = self.create_agent_runnable()
            return await self.run_agent(agent)

        result = await self._run_agent_with_model_remediation(_run_once)
        self._agent_result = result
        return result

    async def json_response(self) -> Data:
        """Produce structured Data via native LLM structured output, with prompt-based fallback.

        Native path (no tools, llm has with_structured_output) bypasses the agent loop and
        returns provider-validated JSON. When tools are attached, falls back to running the
        agent with a schema-augmented system prompt and parsing the final message content.
        """
        from lfx.components.models_and_agents.structured_output.structured_output_orchestrator import (
            orchestrate_structured_output,
        )

        try:
            llm_model, self.chat_history, self.tools = await self.get_agent_requirements()
        except (ValueError, TypeError) as exc:
            await logger.aerror(f"json_response.requirements_failed: {exc}")
            return Data(data={"content": "", "error": str(exc)})

        injected_system_prompt = self._inject_dynamic_prompt_values(getattr(self, "system_prompt", "") or "") or ""
        format_instructions = getattr(self, "format_instructions", "") or ""
        output_schema = getattr(self, "output_schema", None) or []
        has_tools = bool(self.tools)

        async def _run_agent_for_fallback(augmented_prompt: str) -> str:
            first_attempt = True

            async def _run_once() -> Message:
                nonlocal first_attempt, llm_model
                if first_attempt:
                    first_attempt = False
                else:
                    llm_model, self.chat_history, self.tools = await self.get_agent_requirements()
                self.set(
                    llm=llm_model,
                    tools=self.tools or [],
                    chat_history=self.chat_history,
                    input_value=self.input_value,
                    system_prompt=augmented_prompt,
                )
                # Structured output cannot suspend mid-parse: disable tool-approval interrupts.
                agent_runnable = self.create_agent_runnable(allow_interrupts=False)
                return await self.run_agent(agent_runnable)

            with _suppress_send_message(self):
                result = await self._run_agent_with_model_remediation(_run_once)
            return _extract_text_content(result)

        try:
            return await orchestrate_structured_output(
                llm=llm_model,
                output_schema=output_schema,
                system_prompt=injected_system_prompt,
                format_instructions=format_instructions,
                input_value=_extract_text_content(self.input_value),
                run_prompt_fallback=_run_agent_for_fallback,
                prefer_native=not has_tools,
            )
        except (
            ExceptionWithMessageError,
            ValueError,
            TypeError,
            NotImplementedError,
            AttributeError,
        ) as exc:
            await logger.aerror(f"json_response.orchestration_failed: {exc}")
            return Data(data={"content": "", "error": str(exc)})

    async def get_memory_data(self):
        # Scope by flow_id so default playground session names (e.g. "New Session 0")
        # cannot leak chat history across unrelated flows. See issue #13059.
        # The helper also returns [] when n_messages == 0, preserving the
        # explicit "memory disabled" contract from MemoryComponent.retrieve_messages.
        messages = await aget_agent_chat_history(
            session_id=self.graph.session_id,
            flow_id=getattr(self.graph, "flow_id", None),
            context_id=self.context_id,
            n_messages=self.n_messages,
            user_id=_safe_graph_user_id(self),
        )
        return [
            message for message in messages if getattr(message, "id", None) != getattr(self.input_value, "id", None)
        ]

    def update_input_types(self, build_config: dotdict) -> dotdict:
        """Update input types for all fields in build_config."""
        for key, value in build_config.items():
            if isinstance(value, dict):
                if value.get("input_types") is None:
                    build_config[key]["input_types"] = []
            elif hasattr(value, "input_types") and value.input_types is None:
                value.input_types = []
        return build_config

    async def update_build_config(
        self,
        build_config: dotdict,
        field_value: list[dict],
        field_name: str | None = None,
    ) -> dotdict:
        # Update model options with caching (for all field changes).
        # The tool-calling constraint lives on the ModelInput's ``filters``
        # field (declared above); ``handle_model_input_update`` reads it
        # and applies the filter to both the dropdown options and the
        # sticky-default re-injection path.
        build_config = handle_model_input_update(
            component=self,
            build_config=dict(build_config),
            field_value=field_value,
            field_name=field_name,
        )
        build_config = dotdict(build_config)

        if field_name == "model":
            build_config = self.update_input_types(build_config)

            # Validate required keys. `verbose` was dropped from the input set
            # (see `_agent_base_inputs` — the create_agent event stream already
            # surfaces every step), so it is intentionally NOT required here.
            # Saved flows that still carry a `verbose` value just ignore it on
            # load.
            default_keys = [
                "code",
                "_type",
                "model",
                "tools",
                "input_value",
                "add_current_date_tool",
                "add_calculator_tool",
                "system_prompt",
                "max_iterations",
                "handle_parsing_errors",
            ]
            missing_keys = [key for key in default_keys if key not in build_config]
            if missing_keys:
                msg = f"Missing required keys in build_config: {missing_keys}"
                raise ValueError(msg)
        return dotdict({k: v.to_dict() if hasattr(v, "to_dict") else v for k, v in build_config.items()})

    async def _get_tools(self) -> list[Tool]:
        component_toolkit = get_component_toolkit()

        tools = component_toolkit(component=self).get_tools(
            tool_name="Call_Agent",
            # here we do not use the shared callbacks as we are exposing the agent as a tool
            callbacks=self.get_langchain_callbacks(),
        )
        if hasattr(self, "tools_metadata"):
            tools = component_toolkit(component=self, metadata=self.tools_metadata).update_tools_metadata(tools=tools)

        return tools
