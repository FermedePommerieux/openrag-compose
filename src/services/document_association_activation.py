"""Conservative activation wrapper for the existing association semantics."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from models.document_investigation import (
    AssociationDimension,
    DocumentaryNeighborhood,
    DocumentMetadataInspection,
    NeighborhoodLimits,
)
from services.document_investigation import build_documentary_neighborhood

MAX_ACTIVATION_DOCUMENTS = 25
MAX_ACTIVATION_ASSOCIATIONS = 50
MAX_ACTIVATION_PER_DIMENSION = 10

# Production year, source system, MIME/format, collision-prone basename and the
# current combined parent key are intentionally absent.  Parent association can
# be reconsidered only after attachment_of is role-separated from
# member_of/contained_in.
DEFAULT_ACTIVATION_DIMENSIONS = frozenset(
    {
        AssociationDimension.SAME_BINARY_HASH,
        AssociationDimension.SAME_PRODUCTION_INSTANT,
        AssociationDimension.SAME_MODIFICATION_INSTANT,
        AssociationDimension.SAME_PRODUCTION_DAY,
        AssociationDimension.SAME_PRODUCTION_DAY_UTC,
        AssociationDimension.SAME_MODIFICATION_DAY,
        AssociationDimension.SAME_MODIFICATION_DAY_UTC,
        AssociationDimension.SAME_PRODUCTION_MONTH,
        AssociationDimension.SAME_MODIFICATION_MONTH,
        AssociationDimension.SAME_CREATOR_OBSERVATION,
        AssociationDimension.SAME_LAST_MODIFIER_OBSERVATION,
        AssociationDimension.SAME_PRODUCER_OBSERVATION,
        AssociationDimension.SAME_DOCUMENT_TYPE,
    }
)

BLOCKED_STANDALONE_DIMENSIONS = frozenset(
    {
        AssociationDimension.SAME_PARENT_COLLECTION,
        AssociationDimension.SAME_SOURCE_SYSTEM,
        AssociationDimension.SAME_SOURCE_ENTITY_FAMILY,
        AssociationDimension.SAME_PRODUCTION_YEAR,
        AssociationDimension.SAME_PRODUCTION_YEAR_UTC,
        AssociationDimension.SAME_MODIFICATION_YEAR,
        AssociationDimension.SAME_MODIFICATION_YEAR_UTC,
        AssociationDimension.SAME_MIME_TYPE,
        AssociationDimension.COMPATIBLE_DOCUMENT_TYPES,
        AssociationDimension.SAME_EXTENSION,
        AssociationDimension.SAME_FILENAME_BASENAME,
    }
)


def activation_neighborhood_limits(
    *,
    max_documents: int = MAX_ACTIVATION_DOCUMENTS,
    max_associations: int = MAX_ACTIVATION_ASSOCIATIONS,
    per_dimension_limit: int = MAX_ACTIVATION_PER_DIMENSION,
    time_window_days: int | None = None,
    source_scope: tuple[str, ...] = (),
) -> NeighborhoodLimits:
    if not 1 <= max_documents <= MAX_ACTIVATION_DOCUMENTS:
        raise ValueError(f"max_documents must be between 1 and {MAX_ACTIVATION_DOCUMENTS}")
    if not 0 <= max_associations <= MAX_ACTIVATION_ASSOCIATIONS:
        raise ValueError(f"max_associations must be between 0 and {MAX_ACTIVATION_ASSOCIATIONS}")
    if not 1 <= per_dimension_limit <= MAX_ACTIVATION_PER_DIMENSION:
        raise ValueError(
            f"per_dimension_limit must be between 1 and {MAX_ACTIVATION_PER_DIMENSION}"
        )
    return NeighborhoodLimits(
        max_documents=max_documents,
        max_associations=max_associations,
        per_dimension_limit=per_dimension_limit,
        time_window_days=time_window_days,
        source_scope=source_scope,
    )


def build_activation_neighborhood(
    seed_document_ids: Sequence[str],
    inspections: Sequence[DocumentMetadataInspection],
    *,
    accessible_document_ids: set[str],
    dimensions: Iterable[AssociationDimension] | None = None,
    limits: NeighborhoodLimits | None = None,
) -> DocumentaryNeighborhood:
    """Build one DLS-prebounded, non-certifying neighborhood."""
    selected = set(dimensions or DEFAULT_ACTIVATION_DIMENSIONS)
    blocked = sorted(selected & BLOCKED_STANDALONE_DIMENSIONS, key=lambda item: item.value)
    if blocked:
        values = ",".join(item.value for item in blocked)
        raise ValueError(f"mega_hub_dimension_requires_explicit_future_policy: {values}")
    resolved_limits = limits or activation_neighborhood_limits()
    if (
        resolved_limits.max_documents > MAX_ACTIVATION_DOCUMENTS
        or resolved_limits.max_associations > MAX_ACTIVATION_ASSOCIATIONS
        or resolved_limits.per_dimension_limit > MAX_ACTIVATION_PER_DIMENSION
    ):
        raise ValueError("association neighborhood exceeds activation hard bounds")
    neighborhood = build_documentary_neighborhood(
        seed_document_ids,
        inspections,
        accessible_document_ids=accessible_document_ids,
        dimensions=selected,
        limits=resolved_limits,
    )
    if neighborhood.scope_expanding:
        raise RuntimeError("association neighborhood cannot expand certifiable scope")
    return neighborhood
