"""Canonical transport-safe scope certificate semantics (stdlib only)."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

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
