"""Build compact, human-reviewable candidate exports from live captures."""

from __future__ import annotations

import csv
import json
import re
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

REVIEW_FIELDS = (
    "review_state",
    "occurrence_id",
    "document_id",
    "source_entity_id",
    "filename",
    "title",
    "date",
    "sender",
    "recipients",
    "thread",
    "suggested_component_id",
    "source",
    "discovery_origin",
    "depth",
    "path_roles",
    "relations",
    "lexical_rank",
    "dense_rank",
    "rrf_rank",
    "lexical_score",
    "dense_score",
    "rrf_score",
    "text_preview",
    "notes",
)


def _identity(item: dict[str, Any]) -> str:
    return str(item.get("source_entity_id") or item.get("document_id") or "")


def _compact_preview(text: Any, limit: int = 500) -> str:
    if not isinstance(text, str):
        return ""
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _email_header(text: Any, name: str) -> str:
    if not isinstance(text, str):
        return ""
    match = re.search(rf"(?im)^{re.escape(name)}:\s*(.+)$", text)
    return match.group(1).strip() if match else ""


def _provenance(item: dict[str, Any]) -> dict[str, Any]:
    value = item.get("source_provenance")
    return value if isinstance(value, dict) else {}


def _thread(item: dict[str, Any]) -> str:
    relations = _provenance(item).get("relations", [])
    for relation in relations if isinstance(relations, list) else []:
        target = relation.get("target") if isinstance(relation, dict) else None
        if isinstance(target, dict) and target.get("type") == "email_thread":
            return str(target.get("id") or "")
    return ""


def _relation_labels(item: dict[str, Any]) -> list[str]:
    relations = _provenance(item).get("relations", [])
    labels = []
    for relation in relations if isinstance(relations, list) else []:
        if not isinstance(relation, dict):
            continue
        target = relation.get("target")
        target_id = target.get("id") if isinstance(target, dict) else None
        labels.append(f"{relation.get('role', '')}->{target_id or ''}")
    return labels


def _paths(
    seed_entities: set[str], edges: list[dict[str, Any]]
) -> tuple[dict[str, int], dict[str, list[str]]]:
    adjacency: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for edge in edges:
        source = edge.get("source_entity_id")
        target = edge.get("target_entity_id")
        role = str(edge.get("role") or "")
        if isinstance(source, str) and isinstance(target, str):
            adjacency[source].append((target, role))
            adjacency[target].append((source, role))
    for node in adjacency:
        adjacency[node].sort()

    depths = {seed: 0 for seed in sorted(seed_entities)}
    roles: dict[str, list[str]] = {seed: [] for seed in sorted(seed_entities)}
    queue = deque(sorted(seed_entities))
    while queue:
        current = queue.popleft()
        for neighbor, role in adjacency.get(current, []):
            if neighbor in depths:
                continue
            depths[neighbor] = depths[current] + 1
            roles[neighbor] = [*roles[current], role]
            queue.append(neighbor)
    return depths, roles


def build_review_rows(focused: dict[str, Any], scope: dict[str, Any]) -> list[dict[str, Any]]:
    """Return one candidate row per documentary source occurrence."""
    focused_results = [item for item in focused.get("results", []) if isinstance(item, dict)]
    scope_results = [item for item in scope.get("results", []) if isinstance(item, dict)]
    scope_documents = [item for item in scope.get("documents", []) if isinstance(item, dict)]
    graph = scope.get("graph") if isinstance(scope.get("graph"), dict) else {}
    edges = [item for item in graph.get("edges", []) if isinstance(item, dict)]

    seed_by_identity: dict[str, tuple[int, dict[str, Any]]] = {}
    for rank, item in enumerate(focused_results, start=1):
        identity = _identity(item)
        if identity:
            seed_by_identity.setdefault(identity, (rank, item))
    evidence_by_identity: dict[str, dict[str, Any]] = {}
    for item in scope_results:
        identity = _identity(item)
        if not identity:
            continue
        current = evidence_by_identity.get(identity)
        if current is None or int(item.get("chunk_index") or 0) < int(
            current.get("chunk_index") or 0
        ):
            evidence_by_identity[identity] = item

    seed_entities = {identity for identity in seed_by_identity if identity.startswith("urn:")}
    depths, path_roles = _paths(seed_entities, edges)
    rows = []
    for document in scope_documents:
        identity = _identity(document)
        seed = seed_by_identity.get(identity)
        evidence = evidence_by_identity.get(identity, {})
        merged = {**document, **evidence}
        provenance = _provenance(merged)
        entity = provenance.get("entity") if isinstance(provenance.get("entity"), dict) else {}
        text = merged.get("text")
        thread = _thread(merged)
        rank = seed[0] if seed else None
        seed_item = seed[1] if seed else {}
        row = {
            "review_state": "unreviewed",
            "occurrence_id": identity,
            "document_id": merged.get("document_id") or "",
            "source_entity_id": merged.get("source_entity_id") or "",
            "filename": merged.get("filename") or "",
            "title": entity.get("label") or merged.get("source_entity_label") or "",
            "date": (
                entity.get("generated_at_time")
                or merged.get("generated_at_time")
                or _email_header(text, "Date")
            ),
            "sender": _email_header(text, "From"),
            "recipients": "; ".join(
                value for value in (_email_header(text, "To"), _email_header(text, "Cc")) if value
            ),
            "thread": thread,
            "suggested_component_id": thread,
            "source": merged.get("source_entity_system") or merged.get("connector_type") or "",
            "discovery_origin": "seed" if seed else "graph_discovered",
            "depth": depths.get(identity),
            "path_roles": " > ".join(path_roles.get(identity, [])),
            "relations": "; ".join(_relation_labels(merged)),
            "lexical_rank": seed_item.get("lexical_rank"),
            "dense_rank": seed_item.get("dense_rank"),
            "rrf_rank": rank,
            "lexical_score": seed_item.get("lexical_score"),
            "dense_score": seed_item.get("dense_score"),
            "rrf_score": seed_item.get("score") if seed else None,
            "text_preview": _compact_preview(text),
            "notes": "",
        }
        rows.append(row)
    return sorted(rows, key=lambda row: (row["date"] or "9999", row["occurrence_id"]))


def write_review(rows: list[dict[str, Any]], json_path: Path, csv_path: Path) -> None:
    """Write equivalent JSON and spreadsheet-friendly CSV review artifacts."""
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "allowed_review_states": ["relevant", "not_relevant", "uncertain"],
                "candidates": rows,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=REVIEW_FIELDS,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)
