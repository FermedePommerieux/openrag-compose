"""Deterministic GT2 human-review consolidation and completeness gates.

Retrieval and title-family signals can only select rows for human review.  They
never create relevance labels, and a pending selected row blocks the freeze.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from difflib import SequenceMatcher
from typing import Any

HUMAN_LABELS = frozenset({"CORE", "CONTEXTUAL", "NOT_RELEVANT"})
QREL_MAPPING = {"CORE": 2, "CONTEXTUAL": 1, "NOT_RELEVANT": 0}
_GENERIC_TITLE_TOKENS = frozenset(
    {"mail", "email", "eml", "pdf", "document", "documents", "id", "n"}
)


def canonical_sha256(value: Any) -> str:
    """Hash one JSON-compatible value with stable, Unicode-preserving encoding."""

    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _required_text(row: Mapping[str, Any], field: str) -> str:
    value = str(row.get(field) or "").strip()
    if not value:
        raise ValueError(f"human review row requires {field}")
    return value


def validate_human_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    identity_field: str,
    label_field: str = "human_label",
) -> dict[str, Any]:
    """Validate one human-owned row set without resolving conflicts silently."""

    labels_by_identity: dict[str, set[str]] = defaultdict(set)
    identity_counts: Counter[str] = Counter()
    empty_labels: list[str] = []
    invalid_labels: list[dict[str, str]] = []
    for row in rows:
        identity = _required_text(row, identity_field)
        identity_counts[identity] += 1
        label = str(row.get(label_field) or "").strip()
        if not label:
            empty_labels.append(identity)
        elif label not in HUMAN_LABELS:
            invalid_labels.append({identity_field: identity, "human_label": label})
        labels_by_identity[identity].add(label)
    duplicates = sorted(
        identity for identity, count in identity_counts.items() for _ in range(count - 1)
    )
    conflicts = [
        {identity_field: identity, "human_labels": sorted(labels)}
        for identity, labels in sorted(labels_by_identity.items())
        if len(labels) > 1
    ]
    return {
        "rows": len(rows),
        "unique_identities": len(labels_by_identity),
        "label_distribution": dict(
            sorted(Counter(str(row.get(label_field) or "").strip() for row in rows).items())
        ),
        "duplicate_identities": duplicates,
        "conflicts": conflicts,
        "empty_labels": sorted(empty_labels),
        "invalid_labels": invalid_labels,
        "valid": not duplicates and not conflicts and not empty_labels and not invalid_labels,
    }


def consolidate_document_rows(
    stages: Sequence[tuple[str, Sequence[Mapping[str, Any]]]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Merge document-level human stages and fail on duplicates or conflicts."""

    consolidated: list[dict[str, Any]] = []
    for stage, rows in stages:
        for source in rows:
            row = dict(source)
            row.setdefault("review_stage", stage)
            if row["review_stage"] != stage:
                raise ValueError(f"review stage mismatch for {row.get('candidate_id')}")
            consolidated.append(row)
    audit = validate_human_rows(consolidated, identity_field="candidate_id")
    if not audit["valid"]:
        raise ValueError(f"invalid consolidated document reviews: {audit}")
    consolidated.sort(key=lambda row: str(row["candidate_id"]))
    audit["consolidated_qrels_sha256"] = canonical_sha256(consolidated)
    return consolidated, audit


def select_negative_control(
    candidates: Iterable[Mapping[str, Any]],
    *,
    excluded_candidate_ids: set[str],
    sample_size: int,
) -> list[dict[str, Any]]:
    """Select a reproducible one-document-per-component negative control.

    SHA-256 ordering avoids dependence on source row order.  This is a sampling
    rule only; selected rows remain unlabeled until a human reviews them.
    """

    eligible = [
        dict(row)
        for row in candidates
        if _required_text(row, "candidate_id") not in excluded_candidate_ids
    ]
    eligible.sort(
        key=lambda row: (
            hashlib.sha256(str(row["candidate_id"]).encode("utf-8")).hexdigest(),
            str(row["candidate_id"]),
        )
    )
    selected: list[dict[str, Any]] = []
    seen_components: set[str] = set()
    for row in eligible:
        component_id = _required_text(row, "component_id")
        if component_id in seen_components:
            continue
        seen_components.add(component_id)
        selected.append(row)
        if len(selected) == sample_size:
            break
    return selected


def fold_text(value: str) -> str:
    """Return a deterministic case/diacritic-insensitive comparison string."""

    return unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").casefold()


def title_family_signature(title: str) -> str:
    """Normalize volatile identifiers while preserving documentary title shape."""

    value = fold_text(title)
    value = re.sub(r"[0-9a-f]{8,}", "<id>", value)
    value = re.sub(r"\d+", "<n>", value)
    return re.sub(r"\s+", " ", value).strip()


def _semantic_title_tokens(title: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z]+", title_family_signature(title))
        if token not in _GENERIC_TITLE_TOKENS
    }


def generate_title_family_candidates(
    candidates: Iterable[Mapping[str, Any]],
    *,
    judged_rows: Sequence[Mapping[str, Any]],
    similarity_threshold: float = 0.82,
) -> list[dict[str, Any]]:
    """Find unjudged exact/near title families anchored only in human CORE rows."""

    if not 0.0 <= similarity_threshold <= 1.0:
        raise ValueError("similarity_threshold must be between zero and one")
    judged_ids = {_required_text(row, "candidate_id") for row in judged_rows}
    core_anchors = [
        {
            "candidate_id": _required_text(row, "candidate_id"),
            "title": _required_text(row, "title"),
            "signature": title_family_signature(_required_text(row, "title")),
        }
        for row in judged_rows
        if row.get("human_label") == "CORE" and _semantic_title_tokens(str(row.get("title") or ""))
    ]
    selected: list[dict[str, Any]] = []
    for source in candidates:
        candidate_id = _required_text(source, "candidate_id")
        if candidate_id in judged_ids:
            continue
        title = str(source.get("title") or "").strip()
        tokens = _semantic_title_tokens(title)
        if not title or not tokens:
            continue
        signature = title_family_signature(title)
        scored = [
            (
                SequenceMatcher(None, signature, anchor["signature"]).ratio(),
                anchor,
            )
            for anchor in core_anchors
            if tokens & _semantic_title_tokens(anchor["title"])
        ]
        if not scored:
            continue
        similarity, anchor = max(
            scored,
            key=lambda item: (item[0], item[1]["candidate_id"]),
        )
        if similarity < similarity_threshold:
            continue
        selected.append(
            {
                **dict(source),
                "selection_class": (
                    "EXACT_HUMAN_CORE_TITLE_FAMILY"
                    if signature == anchor["signature"]
                    else "NEAR_HUMAN_CORE_TITLE_FAMILY"
                ),
                "selection_reason": (
                    "Le titre du document non jugé appartient à une famille structurelle "
                    "exacte ou proche d'un titre CORE humain ; une revue humaine est requise."
                ),
                "anchor_candidate_id": anchor["candidate_id"],
                "anchor_title": anchor["title"],
                "title_family_signature": signature,
                "title_similarity": similarity,
                "human_label": "",
            }
        )
    selected.sort(key=lambda row: str(row["candidate_id"]))
    return selected


def freeze_gate(
    *,
    consolidation_audit: Mapping[str, Any],
    component_audit: Mapping[str, Any],
    pending_high_priority: Sequence[Mapping[str, Any]],
    negative_control_complete: bool,
    guideline_version: str,
) -> dict[str, Any]:
    """Evaluate the fail-closed GT2 freeze contract."""

    blockers: list[str] = []
    if consolidation_audit.get("valid") is not True:
        blockers.append("document_qrels_invalid")
    if component_audit.get("valid") is not True:
        blockers.append("component_judgments_invalid")
    if pending_high_priority:
        blockers.append("high_priority_human_review_pending")
    if not negative_control_complete:
        blockers.append("negative_control_incomplete")
    if not guideline_version.strip():
        blockers.append("guideline_version_missing")
    return {
        "GT2_FREEZE": "BLOCKED" if blockers else "PASS",
        "blockers": blockers,
        "pending_high_priority_candidates": len(pending_high_priority),
        "negative_control_complete": negative_control_complete,
        "guideline_version": guideline_version,
        "fail_closed": True,
    }


def standard_ir_metrics(
    ranked_candidate_ids: Sequence[str],
    qrels: Mapping[str, int],
) -> dict[str, float | None]:
    """Compute standard graded/binary IR metrics for one deterministic run."""

    ranked = list(dict.fromkeys(ranked_candidate_ids))
    relevant = {candidate_id for candidate_id, grade in qrels.items() if grade > 0}

    def dcg(k: int) -> float:
        return sum(
            (2 ** qrels.get(candidate_id, 0) - 1) / math.log2(rank + 1)
            for rank, candidate_id in enumerate(ranked[:k], start=1)
        )

    ideal_grades = sorted(qrels.values(), reverse=True)

    def idcg(k: int) -> float:
        return sum(
            (2**grade - 1) / math.log2(rank + 1)
            for rank, grade in enumerate(ideal_grades[:k], start=1)
        )

    precisions = []
    relevant_seen = 0
    for rank, candidate_id in enumerate(ranked, start=1):
        if candidate_id in relevant:
            relevant_seen += 1
            precisions.append(relevant_seen / rank)

    def recall(k: int) -> float | None:
        if not relevant:
            return None
        return len(set(ranked[:k]) & relevant) / len(relevant)

    def precision(k: int) -> float:
        return len(set(ranked[:k]) & relevant) / k

    ideal10 = idcg(10)
    ideal100 = idcg(100)
    return {
        "nDCG@10": dcg(10) / ideal10 if ideal10 else None,
        "nDCG@100": dcg(100) / ideal100 if ideal100 else None,
        "MAP": sum(precisions) / len(relevant) if relevant else None,
        "Recall@100": recall(100),
        "Recall@200": recall(200),
        "Precision@100": precision(100),
    }


def condensed_standard_ir_metrics(
    ranked_candidate_ids: Sequence[str],
    qrels: Mapping[str, int],
) -> dict[str, float | int | None]:
    """Evaluate a condensed judged-only ranking without demoting unjudged rows.

    TREC-style pooling often maps unjudged rows to grade zero.  GT2 explicitly
    forbids that assumption, so unknown identities are removed before scoring
    and reported separately.  Precision uses only the judged documents present
    in the condensed top 100 as its denominator.
    """

    ranked = list(dict.fromkeys(ranked_candidate_ids))
    judged = [candidate_id for candidate_id in ranked if candidate_id in qrels]
    metrics = standard_ir_metrics(judged, qrels)
    relevant = {candidate_id for candidate_id, grade in qrels.items() if grade > 0}
    precision_denominator = min(100, len(judged))
    metrics["Precision@100"] = (
        len(set(judged[:100]) & relevant) / precision_denominator
        if precision_denominator
        else None
    )
    return {
        **metrics,
        "ranked_unique_documents": len(ranked),
        "evaluated_judged_documents": len(judged),
        "unjudged_documents_excluded": len(ranked) - len(judged),
    }
