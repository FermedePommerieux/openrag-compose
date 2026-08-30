"""Pure regressions for the embedded Langflow retrieval guard.

The Agent component runs inside the Langflow image, whose ``lfx`` dependency is
not part of the backend unit environment. These tests execute the guard's pure
definitions directly from the canonical component source so the tested logic
is exactly what is embedded into the shipped flow.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
AGENT_SOURCE = ROOT / "flows" / "components" / "openrag_agent.py"


class _FakeToolMessage:
    def __init__(self, content, tool_call_id, name=None, status=None):
        self.content = content
        self.tool_call_id = tool_call_id
        self.name = name
        self.status = status
        self.type = "tool"


class _FakeAgentMiddleware:
    pass


class _FakeLogger:
    def info(self, _message):
        return None


def _load_guard_namespace() -> dict[str, Any]:
    tree = ast.parse(AGENT_SOURCE.read_text(encoding="utf-8"))
    functions = {
        "_canonical_hash",
        "_normalize_retrieval_intent",
        "_message_value",
        "_message_tool_calls",
        "_is_current_run_user_message",
        "_current_run_messages",
        "_retrieval_mode",
        "_tool_name",
        "_retrieval_guard_context",
        "_find_retrieval_guard_context",
        "_parse_tool_payload",
        "_evidence_identities",
        "_coverage_state",
        "_call_args",
        "_retrieval_call_keys",
        "_build_retrieval_guard_snapshot",
        "_retrieval_guard_reason",
        "_blocked_retrieval_message",
        "_compute_agent_recursion_budget",
    }
    classes = {
        "_RetrievalRecord",
        "_RetrievalGuardSnapshot",
        "OpenRAGRetrievalGuardMiddleware",
    }
    selected: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [target.id for target in targets if isinstance(target, ast.Name)]
            if any(name.startswith("_RETRIEVAL_") for name in names):
                selected.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in functions:
            selected.append(node)
        elif isinstance(node, ast.ClassDef) and node.name in classes:
            selected.append(node)

    namespace = {
        "Any": Any,
        "AgentMiddleware": _FakeAgentMiddleware,
        "ToolMessage": _FakeToolMessage,
        "dataclass": dataclass,
        "field": field,
        "hashlib": hashlib,
        "json": json,
        "logger": _FakeLogger(),
        "re": re,
        "unicodedata": unicodedata,
    }
    module = ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[]))
    exec(compile(module, AGENT_SOURCE.as_posix(), "exec"), namespace)
    return namespace


GUARD = _load_guard_namespace()
CONTEXT = {
    "filter_fingerprint": "filters-a",
    "scope_policy_id": "documentary-prov-o",
    "scope_policy_version": 1,
}


def _user(text="request"):
    return SimpleNamespace(type="human", content=text)


def _ai(call_id, query, *, scope_exhaustive=False, tool_name="search_documents"):
    return SimpleNamespace(
        type="ai",
        tool_calls=[
            {
                "id": call_id,
                "name": tool_name,
                "args": {
                    "search_query": query,
                    "read_document_id": "",
                    "cursor": "",
                    "scope_exhaustive": scope_exhaustive,
                },
            }
        ],
    )


def _tool(call_id, *, chunks=(), documents=(), coverage=None, name="search_documents"):
    payload = {
        "results": [
            {"document_id": document_id, "chunk_id": chunk_id} for document_id, chunk_id in chunks
        ],
        "documents": [
            {"document_id": document_id, "source_entity_id": occurrence}
            for document_id, occurrence in documents
        ],
    }
    if coverage is not None:
        payload["coverage"] = coverage
    return _FakeToolMessage(json.dumps(payload), call_id, name=name, status="success")


def _snapshot(messages, context=CONTEXT):
    return GUARD["_build_retrieval_guard_snapshot"](messages, context)


def test_normalized_intent_is_order_accent_and_plural_insensitive():
    normalize = GUARD["_normalize_retrieval_intent"]

    normalized = normalize("Documents associés au dossier 26050004")
    assert normalized == normalize("dossier n°26050004 : document associes")
    assert normalized != normalize("Documents associés au dossier 26080032")
    assert normalize("facture 26050004 total TTC") == normalize("montant TTC facture n°26050004")
    assert len(normalized) == 64
    assert "document" not in normalized


def test_result_fingerprint_ignores_ranking_and_serialization_order():
    first = _snapshot(
        [
            _user(),
            _ai("a", "invoice total"),
            _tool("a", chunks=(("doc-1", "chunk-1"), ("doc-2", "chunk-2"))),
        ]
    ).records[-1]
    second = _snapshot(
        [
            _user(),
            _ai("b", "invoice total"),
            _tool("b", chunks=(("doc-2", "chunk-2"), ("doc-1", "chunk-1"))),
        ]
    ).records[-1]

    assert first.result_fingerprint == second.result_fingerprint
    assert len(first.retrieval_fingerprint) == 64


def test_scope_complete_marks_terminal_but_allows_distinct_focused_search():
    messages = [
        _user(),
        _ai("scope", "toutes les factures liées à l'abattage", scope_exhaustive=True),
        _tool(
            "scope",
            chunks=(("doc-1", "chunk-1"),),
            coverage={
                "mode": "scope_exhaustive",
                "complete": True,
                "status_code": "complete",
                "failure_codes": [],
                "scope_policy_id": "documentary-prov-o",
                "scope_policy_version": 1,
            },
        ),
    ]
    snapshot = _snapshot(messages)
    repeated = _ai("repeat", "abattage : les factures liées", scope_exhaustive=True).tool_calls[0]
    focused = _ai("focused", "facture 26050004 total TTC").tool_calls[0]

    assert snapshot.exhaustive_scope_satisfied is True
    assert GUARD["_retrieval_guard_reason"](snapshot, repeated, CONTEXT) == (
        "scope_already_complete"
    )
    assert GUARD["_retrieval_guard_reason"](snapshot, focused, CONTEXT) is None


def test_incomplete_scope_keeps_distinct_recovery_available_and_fail_closed():
    messages = [
        _user(),
        _ai("scope", "toutes les factures", scope_exhaustive=True),
        _tool(
            "scope",
            chunks=(("doc-1", "chunk-1"),),
            coverage={
                "mode": "scope_exhaustive",
                "complete": False,
                "status_code": "seed_missing_provenance",
                "failure_codes": ["seed_missing_provenance"],
            },
        ),
    ]
    snapshot = _snapshot(messages)
    recovery = _ai("focused", "facture 26050004 total TTC").tool_calls[0]

    assert snapshot.exhaustive_scope_satisfied is False
    assert snapshot.stalled is False
    assert snapshot.records[-1].coverage["complete"] is False
    assert GUARD["_retrieval_guard_reason"](snapshot, recovery, CONTEXT) is None


def test_incomplete_scope_without_progress_closes_retrieval_with_limitations():
    messages = [
        _user(),
        _ai("scope", "documents du dossier", scope_exhaustive=True),
        _tool(
            "scope",
            chunks=(("doc-1", "chunk-1"),),
            coverage={
                "mode": "scope_exhaustive",
                "complete": False,
                "status_code": "access_error",
                "failure_codes": ["access_error"],
            },
        ),
        _ai("focused-a", "preuve précise dossier"),
        _tool("focused-a", chunks=(("doc-1", "chunk-1"),)),
        _ai("focused-b", "preuve du dossier reformulée"),
        _tool("focused-b", chunks=(("doc-1", "chunk-1"),)),
    ]

    snapshot = _snapshot(messages)
    assert snapshot.exhaustive_scope_satisfied is False
    assert snapshot.stalled is True
    assert snapshot.guard_reason == "retrieval_no_progress"
    blocked = GUARD["_blocked_retrieval_message"](
        _ai("blocked", "encore une recherche").tool_calls[0],
        snapshot.guard_reason,
        snapshot.latest_normalized_intent,
    )
    guard_payload = json.loads(blocked.content)["openrag_retrieval_guard"]
    assert guard_payload["reason"] == "retrieval_no_progress"
    assert "limitations preserved" in guard_payload["message"]


def test_same_query_and_same_results_stalls_retrieval():
    messages = [
        _user(),
        _ai("a", "facture 26050004 total TTC"),
        _tool("a", chunks=(("doc-1", "chunk-1"),)),
        _ai("b", "facture 26050004 total TTC"),
        _tool("b", chunks=(("doc-1", "chunk-1"),)),
    ]

    snapshot = _snapshot(messages)
    assert snapshot.stalled is True
    assert snapshot.guard_reason == "retrieval_no_progress"
    assert snapshot.latest_wave_progress is False


def test_rephrased_same_intent_and_results_stalls_retrieval():
    messages = [
        _user(),
        _ai("a", "facture 26050004 total TTC"),
        _tool("a", chunks=(("doc-1", "chunk-1"),)),
        _ai("b", "montant TTC facture n°26050004"),
        _tool("b", chunks=(("doc-1", "chunk-1"),)),
    ]

    snapshot = _snapshot(messages)
    # The stable identifier anchors the intent without any business synonym
    # table; unchanged evidence then proves that the reformulation made no progress.
    assert snapshot.records[0].normalized_intent == snapshot.records[1].normalized_intent
    assert snapshot.records[1].progress is False
    assert snapshot.stalled is True


def test_new_evidence_and_enriched_result_are_progress():
    messages = [
        _user(),
        _ai("a", "facture A total TTC"),
        _tool("a", chunks=(("doc-a", "chunk-a"),)),
        _ai("b", "facture B total TTC"),
        _tool("b", chunks=(("doc-a", "chunk-a"), ("doc-b", "chunk-b"))),
    ]

    snapshot = _snapshot(messages)
    assert snapshot.records[-1].progress is True
    assert snapshot.stalled is False


def test_filter_fingerprint_changes_effective_scope_and_allows_same_terms():
    call = _ai("a", "facture 26050004 total TTC").tool_calls[0]
    other_context = {**CONTEXT, "filter_fingerprint": "filters-b"}

    first_keys = GUARD["_retrieval_call_keys"](call, CONTEXT)
    second_keys = GUARD["_retrieval_call_keys"](call, other_context)
    assert first_keys[3] != second_keys[3]
    assert first_keys[4] != second_keys[4]


def test_invoice_regression_stalls_after_repeated_evidence_then_preserves_calculator():
    messages = [
        _user("Somme toutes les factures liées à l'abattage des porcs"),
        _ai("scope", "factures abattage porc", scope_exhaustive=True),
        _tool(
            "scope",
            chunks=(("scope-doc", "scope-chunk"),),
            coverage={
                "mode": "scope_exhaustive",
                "complete": True,
                "status_code": "complete",
                "failure_codes": [],
            },
        ),
        _ai("a", "facture 00112901 total TTC"),
        _tool("a", chunks=(("invoice-a", "amount-a"),)),
        _ai("b", "facture 00116892 total TTC"),
        _tool("b", chunks=(("invoice-b", "amount-b"),)),
        _ai("c", "montant TTC facture n°00116892"),
        _tool("c", chunks=(("invoice-b", "amount-b"),)),
    ]
    snapshot = _snapshot(messages)
    attempted = _ai("d", "total de la facture 00116892 TTC").tool_calls[0]

    assert snapshot.stalled is True
    assert GUARD["_retrieval_guard_reason"](snapshot, attempted, CONTEXT) == (
        "retrieval_no_progress"
    )

    middleware = GUARD["OpenRAGRetrievalGuardMiddleware"]()

    class _Request:
        def __init__(self, tools):
            self.tools = tools
            self.tool_choice = "search_documents"
            self.state = {"messages": messages}

        def override(self, **changes):
            clone = _Request(changes.get("tools", self.tools))
            clone.tool_choice = changes.get("tool_choice", self.tool_choice)
            clone.state = self.state
            return clone

    guarded = middleware._guard_model_request(
        _Request(
            [
                SimpleNamespace(name="search_documents", metadata={}),
                SimpleNamespace(name="evaluate_expression", metadata={}),
            ]
        )
    )
    assert [tool.name for tool in guarded.tools] == ["evaluate_expression"]
    assert guarded.tool_choice is None

    calculator_messages = [
        *messages,
        _ai("calc", "156.49+150.85", tool_name="evaluate_expression"),
        _tool("calc", chunks=(), name="evaluate_expression"),
        SimpleNamespace(type="ai", tool_calls=[], content="307.34"),
    ]
    assert len(_snapshot(calculator_messages).records) == 4


def test_multiple_legitimate_focused_searches_remain_available():
    messages = [_user()]
    for call_id, document_id in (("a", "invoice-a"), ("b", "invoice-b"), ("c", "invoice-c")):
        messages.extend(
            [
                _ai(call_id, f"facture {document_id} total TTC"),
                _tool(call_id, chunks=((document_id, f"amount-{call_id}"),)),
            ]
        )

    snapshot = _snapshot(messages)
    assert [record.progress for record in snapshot.records] == [True, True, True]
    assert snapshot.stalled is False


def test_graph_budget_uses_real_middleware_topology_and_terminal_overhead():
    budget = GUARD["_compute_agent_recursion_budget"]
    nodes = {
        "__start__",
        "ModelCallLimitMiddleware.before_model",
        "model",
        "ModelCallLimitMiddleware.after_model",
        "tools",
        "__end__",
    }

    assert budget(15, nodes) == 62
    final_answer_end_step = (9 - 1) * 4 + 4
    assert final_answer_end_step == 36
    assert budget(9, nodes) > final_answer_end_step
    assert budget(0, nodes) == 6


def test_guard_uses_wrappers_without_adding_graph_hook_nodes():
    tree = ast.parse(AGENT_SOURCE.read_text(encoding="utf-8"))
    middleware = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "OpenRAGRetrievalGuardMiddleware"
    )
    method_names = {
        node.name
        for node in middleware.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert {"wrap_model_call", "awrap_model_call", "wrap_tool_call", "awrap_tool_call"} <= (
        method_names
    )
    assert "before_model" not in method_names
    assert "after_model" not in method_names
