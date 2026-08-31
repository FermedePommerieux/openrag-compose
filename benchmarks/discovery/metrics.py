"""Pure, deterministic discovery and post-PROV-O metric computation."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Any


def occurrence_identity(item: dict[str, Any]) -> str | None:
    """Return the source occurrence identity, never a chunk identity."""
    for field in ("occurrence_id", "source_entity_id"):
        value = item.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _ratio(numerator: int, denominator: int, *, pending_status: str) -> dict[str, Any]:
    return {
        "value": numerator / denominator if denominator else None,
        "numerator": numerator,
        "denominator": denominator,
        "status": "measured" if denominator else pending_status,
    }


def _ground_truth_index(definition: dict[str, Any]) -> dict[str, Any]:
    documents = definition.get("documents", [])
    by_occurrence = {item["occurrence_id"]: item for item in documents}
    by_source_entity = {
        item["source_entity_id"]: item["occurrence_id"]
        for item in documents
        if item.get("source_entity_id")
    }
    by_document_id: dict[str, list[str]] = defaultdict(list)
    for item in documents:
        by_document_id[item["document_id"]].append(item["occurrence_id"])
    unambiguous_document_ids = {
        document_id: occurrence_ids[0]
        for document_id, occurrence_ids in by_document_id.items()
        if len(occurrence_ids) == 1
    }
    return {
        "by_occurrence": by_occurrence,
        "by_source_entity": by_source_entity,
        "by_document_id": unambiguous_document_ids,
    }


def _match_occurrence(item: dict[str, Any], index: dict[str, Any]) -> str | None:
    identity = occurrence_identity(item)
    if identity in index["by_occurrence"]:
        return identity
    if identity in index["by_source_entity"]:
        return index["by_source_entity"][identity]
    document_id = item.get("document_id")
    if isinstance(document_id, str):
        return index["by_document_id"].get(document_id)
    return None


def _unique_occurrences(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for item in items:
        identity = occurrence_identity(item)
        if identity is None:
            document_id = item.get("document_id")
            identity = f"document:{document_id}" if document_id else None
        if identity is not None:
            unique.setdefault(identity, item)
    return list(unique.values())


def _best_rank(
    member_chunks: list[tuple[int, dict[str, Any], str | None]], field: str
) -> int | None:
    values = [int(item[field]) for _, item, _ in member_chunks if isinstance(item.get(field), int)]
    return min(values) if values else None


def _compact_seed_capture(seed_chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fields = (
        "chunk_id",
        "chunk_index",
        "document_id",
        "lexical_rank",
        "dense_rank",
        "rrf_rank",
        "lexical_score",
        "dense_score",
        "rrf_score",
    )
    return [
        {
            "rank": rank,
            "occurrence_id": occurrence_identity(item),
            **{field: item.get(field) for field in fields},
        }
        for rank, item in enumerate(seed_chunks, start=1)
    ]


def compute_metrics(
    definition: dict[str, Any],
    seed_chunks: list[dict[str, Any]],
    *,
    k: int,
    closure_documents: list[dict[str, Any]] | None = None,
    coverage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute strict metrics for a top-K chunk seed budget.

    ``K`` follows the production retrieval contract and therefore counts ranked
    seed chunks. Document metrics de-duplicate those chunks by source occurrence.
    ``uncertain`` and unreviewed/unlisted occurrences are excluded from strict
    precision; the returned review coverage says whether precision is final.
    """
    pending = "pending_human_ground_truth_validation"
    index = _ground_truth_index(definition)
    ground_truth_documents = index["by_occurrence"]
    relevant_occurrences = {
        occurrence_id
        for occurrence_id, item in ground_truth_documents.items()
        if item["state"] == "relevant"
    }
    relevant_components = {
        item["component_id"]
        for item in definition.get("components", [])
        if item["state"] == "relevant"
    }

    selected_seed_chunks = seed_chunks[: max(0, k)]
    seed_occurrences = _unique_occurrences(selected_seed_chunks)
    closure_occurrences = (
        _unique_occurrences(closure_documents or []) if closure_documents is not None else None
    )

    matched_seed = {
        occurrence_id
        for item in seed_occurrences
        if (occurrence_id := _match_occurrence(item, index)) is not None
    }
    matched_closure = (
        {
            occurrence_id
            for item in closure_occurrences or []
            if (occurrence_id := _match_occurrence(item, index)) is not None
        }
        if closure_occurrences is not None
        else None
    )
    seed_relevant = matched_seed & relevant_occurrences
    closure_relevant = (
        matched_closure & relevant_occurrences if matched_closure is not None else None
    )

    component_by_occurrence = {
        occurrence_id: item.get("component_id")
        for occurrence_id, item in ground_truth_documents.items()
    }
    component_type_by_id = {
        item["component_id"]: item["type"] for item in definition.get("components", [])
    }
    seeded_components = {
        component_by_occurrence[item]
        for item in seed_relevant
        if component_by_occurrence.get(item) in relevant_components
    }
    closure_components = (
        {
            component_by_occurrence[item]
            for item in closure_relevant or set()
            if component_by_occurrence.get(item) in relevant_components
        }
        if closure_relevant is not None
        else None
    )

    classified_seed = {
        occurrence_id
        for occurrence_id in matched_seed
        if ground_truth_documents[occurrence_id]["state"] in {"relevant", "not_relevant"}
    }
    uncertain_seed = {
        occurrence_id
        for occurrence_id in matched_seed
        if ground_truth_documents[occurrence_id]["state"] == "uncertain"
    }
    unclassified_seed_count = len(seed_occurrences) - len(matched_seed)

    seed_doc_recall = _ratio(len(seed_relevant), len(relevant_occurrences), pending_status=pending)
    seed_component_recall = _ratio(
        len(seeded_components), len(relevant_components), pending_status=pending
    )
    precision = _ratio(len(seed_relevant), len(classified_seed), pending_status=pending)
    precision["review_coverage"] = (
        len(classified_seed) / len(seed_occurrences) if seed_occurrences else None
    )
    precision["reliable"] = (
        bool(seed_occurrences) and not uncertain_seed and not (unclassified_seed_count)
    )
    precision["uncertain_seed_occurrences"] = len(uncertain_seed)
    precision["unclassified_seed_occurrences"] = unclassified_seed_count

    if closure_occurrences is None:
        post_document_recall = {
            "value": None,
            "numerator": None,
            "denominator": len(relevant_occurrences),
            "status": "not_measured_exact_seed_set",
        }
        post_component_recall = {
            "value": None,
            "numerator": None,
            "denominator": len(relevant_components),
            "status": "not_measured_exact_seed_set",
        }
        expansion_per_seed = None
        expansion_per_relevant = None
        recovery_gain = None
        recovery_multiplier = None
    else:
        post_document_recall = _ratio(
            len(closure_relevant or set()), len(relevant_occurrences), pending_status=pending
        )
        post_component_recall = _ratio(
            len(closure_components or set()), len(relevant_components), pending_status=pending
        )
        expansion_per_seed = (
            len(closure_occurrences) / len(seed_occurrences) if seed_occurrences else None
        )
        expansion_per_relevant = (
            len(closure_occurrences) / len(closure_relevant) if closure_relevant else None
        )
        recovery_gain = len(closure_relevant or set()) - len(seed_relevant)
        recovery_multiplier = (
            len(closure_relevant or set()) / len(seed_relevant) if seed_relevant else None
        )

    component_rows = []
    for component in sorted(
        (item for item in definition.get("components", []) if item["state"] == "relevant"),
        key=lambda item: item["component_id"],
    ):
        members = set(component["required_occurrence_ids"])
        relevant_members = members & relevant_occurrences
        seed_member_chunks = [
            (rank, item, _match_occurrence(item, index))
            for rank, item in enumerate(selected_seed_chunks, start=1)
            if _match_occurrence(item, index) in relevant_members
        ]
        first_seed = seed_member_chunks[0] if seed_member_chunks else None
        lexical = any(item.get("lexical_rank") is not None for _, item, _ in seed_member_chunks)
        dense = any(item.get("dense_rank") is not None for _, item, _ in seed_member_chunks)
        channel = (
            "both" if lexical and dense else "lexical" if lexical else "dense" if dense else "none"
        )
        all_member_chunks = [
            (rank, item, _match_occurrence(item, index))
            for rank, item in enumerate(seed_chunks, start=1)
            if _match_occurrence(item, index) in relevant_members
        ]

        best_rrf_rank = _best_rank(all_member_chunks, "rrf_rank")
        if best_rrf_rank is None and all_member_chunks:
            best_rrf_rank = min(rank for rank, _, _ in all_member_chunks)
        missed = not seed_member_chunks
        component_rows.append(
            {
                "component_id": component["component_id"],
                "documents": sorted(relevant_members),
                "isolated_or_connected": (
                    "isolated" if component.get("type") == "standalone_document" else "connected"
                ),
                "seeded": bool(seed_member_chunks),
                "first_seed_rank": first_seed[0] if first_seed else None,
                "first_seed_occurrence_id": first_seed[2] if first_seed else None,
                "first_seed_document_id": (
                    first_seed[1].get("document_id") if first_seed else None
                ),
                "retrieval_channel": channel,
                "best_lexical_rank": _best_rank(all_member_chunks, "lexical_rank"),
                "best_dense_rank": _best_rank(all_member_chunks, "dense_rank"),
                "best_rrf_rank": best_rrf_rank,
                "present_outside_k": any(rank > k for rank, _, _ in all_member_chunks),
                "miss_analysis": {
                    "query_vocabulary_mismatch": None,
                    "metadata_mismatch": None,
                    "semantic_mismatch": None,
                    "index_profile_issue": None,
                    "cause": "unknown" if missed else None,
                },
                "reached_after_closure": (
                    bool(relevant_members & (matched_closure or set()))
                    if matched_closure is not None
                    else None
                ),
            }
        )

    document_rows = []
    for occurrence_id in sorted(relevant_occurrences):
        document = ground_truth_documents[occurrence_id]
        all_document_chunks = [
            (rank, item, _match_occurrence(item, index))
            for rank, item in enumerate(seed_chunks, start=1)
            if _match_occurrence(item, index) == occurrence_id
        ]
        selected_document_chunks = [item for item in all_document_chunks if item[0] <= max(0, k)]
        best_rrf_rank = _best_rank(all_document_chunks, "rrf_rank")
        if best_rrf_rank is None and all_document_chunks:
            best_rrf_rank = min(rank for rank, _, _ in all_document_chunks)
        component_id = document.get("component_id")
        missed = not selected_document_chunks
        document_rows.append(
            {
                "occurrence_id": occurrence_id,
                "document_id": document["document_id"],
                "component_id": component_id,
                "isolated_or_connected": (
                    "isolated"
                    if component_type_by_id.get(component_id) == "standalone_document"
                    else "connected"
                ),
                "seeded": not missed,
                "first_seed_rank": (
                    selected_document_chunks[0][0] if selected_document_chunks else None
                ),
                "best_lexical_rank": _best_rank(all_document_chunks, "lexical_rank"),
                "best_dense_rank": _best_rank(all_document_chunks, "dense_rank"),
                "best_rrf_rank": best_rrf_rank,
                "present_outside_k": any(rank > k for rank, _, _ in all_document_chunks),
                "miss_analysis": {
                    "query_vocabulary_mismatch": None,
                    "metadata_mismatch": None,
                    "semantic_mismatch": None,
                    "index_profile_issue": None,
                    "cause": "unknown" if missed else None,
                },
                "reached_after_closure": (
                    occurrence_id in (matched_closure or set())
                    if matched_closure is not None
                    else None
                ),
            }
        )

    return {
        "k_unit": "ranked_seed_chunks",
        "requested_k": k,
        "available_seed_chunks": len(seed_chunks),
        "effective_seed_chunks": len(selected_seed_chunks),
        "seed_occurrences": len(seed_occurrences),
        "seed_document_ids": sorted(
            {str(item["document_id"]) for item in seed_occurrences if item.get("document_id")}
        ),
        "seed_occurrence_ids": sorted(
            {
                identity
                for item in seed_occurrences
                if (identity := occurrence_identity(item)) is not None
            }
        ),
        "seed_capture": _compact_seed_capture(selected_seed_chunks),
        "closure_occurrences": len(closure_occurrences)
        if closure_occurrences is not None
        else None,
        "seed_document_recall": seed_doc_recall,
        "seed_component_recall": seed_component_recall,
        "post_prov_o_document_recall": post_document_recall,
        "post_prov_o_component_recall": post_component_recall,
        "precision": precision,
        "expansion_factor_per_seed_occurrence": expansion_per_seed,
        "expansion_factor_per_relevant_recovered": expansion_per_relevant,
        "document_recovery_gain": recovery_gain,
        "recovery_multiplier": recovery_multiplier,
        "coverage_complete": coverage.get("complete") if coverage else None,
        "coverage_status_code": coverage.get("status_code") if coverage else None,
        "coverage_failure_codes": coverage.get("failure_codes", []) if coverage else [],
        "document_analysis": document_rows,
        "component_analysis": component_rows,
    }


def coverage_success_rate(runs: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Compute coverage success independently from relevance recall."""
    scope_runs = [run for run in runs if run.get("coverage_complete") is not None]
    successful = sum(run.get("coverage_complete") is True for run in scope_runs)
    return _ratio(successful, len(scope_runs), pending_status="not_measured")
