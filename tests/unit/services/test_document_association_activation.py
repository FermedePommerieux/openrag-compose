from __future__ import annotations

from datetime import UTC, datetime

import pytest

from models.document_investigation import AssociationDimension, NeighborhoodCompleteness
from models.document_metadata import (
    DocumentMetadataProfile,
    MetadataNormalizationStatus,
    MetadataObservation,
    MetadataSectionName,
    MetadataSourceType,
    MetadataTrustClass,
)
from services.document_association_activation import (
    DEFAULT_ACTIVATION_DIMENSIONS,
    activation_neighborhood_limits,
    build_activation_neighborhood,
)
from services.document_investigation import inspect_document_metadata


def _inspection(document_id: str, day: int, *, creator: str = "Alice"):
    observations = [
        MetadataObservation(
            section=MetadataSectionName.EMBEDDED,
            field="embedded_created_at",
            value=f"2024-03-{day:02d}T00:00:00+00:00",
            raw_value=f"2024-03-{day:02d}T00:00:00Z",
            source="pdf_info_dictionary",
            source_type=MetadataSourceType.FORMAT_NATIVE,
            trust_class=MetadataTrustClass.EMBEDDED_DOCUMENT_METADATA,
            extracted_at=datetime(2026, 9, 3, tzinfo=UTC),
            normalization_status=MetadataNormalizationStatus.TIMEZONE_EXPLICIT,
            timezone="Z",
        ),
        MetadataObservation(
            section=MetadataSectionName.EMBEDDED,
            field="creator",
            value=creator,
            source="pdf_info_dictionary",
            source_type=MetadataSourceType.FORMAT_NATIVE,
            trust_class=MetadataTrustClass.EMBEDDED_DOCUMENT_METADATA,
            extracted_at=datetime(2026, 9, 3, tzinfo=UTC),
        ),
    ]
    return inspect_document_metadata(
        DocumentMetadataProfile(entity_id=document_id, embedded=observations)
    )


def test_activation_defaults_exclude_audited_mega_hub_dimensions():
    assert AssociationDimension.SAME_PARENT_COLLECTION not in DEFAULT_ACTIVATION_DIMENSIONS
    assert AssociationDimension.SAME_SOURCE_SYSTEM not in DEFAULT_ACTIVATION_DIMENSIONS
    assert AssociationDimension.SAME_PRODUCTION_YEAR not in DEFAULT_ACTIVATION_DIMENSIONS
    assert AssociationDimension.SAME_MIME_TYPE not in DEFAULT_ACTIVATION_DIMENSIONS
    assert AssociationDimension.SAME_FILENAME_BASENAME not in DEFAULT_ACTIVATION_DIMENSIONS


def test_activation_limits_are_hard_bounded():
    with pytest.raises(ValueError, match="max_documents"):
        activation_neighborhood_limits(max_documents=26)
    with pytest.raises(ValueError, match="per_dimension_limit"):
        activation_neighborhood_limits(per_dimension_limit=11)


def test_parent_collection_requires_a_future_role_safe_policy():
    seed = _inspection("seed", 1)
    neighbor = _inspection("neighbor", 2)

    with pytest.raises(ValueError, match="mega_hub_dimension"):
        build_activation_neighborhood(
            [seed.document_id],
            [seed, neighbor],
            accessible_document_ids={seed.document_id, neighbor.document_id},
            dimensions={AssociationDimension.SAME_PARENT_COLLECTION},
        )


def test_activation_neighborhood_is_dls_bounded_non_certifying_and_deterministic():
    seed = _inspection("seed", 1)
    visible = _inspection("visible", 2)
    hidden = _inspection("hidden", 3)
    inspections = [hidden, visible, seed]
    accessible = {seed.document_id, visible.document_id}

    first = build_activation_neighborhood(
        [seed.document_id],
        inspections,
        accessible_document_ids=accessible,
        limits=activation_neighborhood_limits(
            max_documents=2,
            max_associations=1,
            per_dimension_limit=1,
        ),
    )
    second = build_activation_neighborhood(
        [seed.document_id],
        list(reversed(inspections)),
        accessible_document_ids=accessible,
        limits=activation_neighborhood_limits(
            max_documents=2,
            max_associations=1,
            per_dimension_limit=1,
        ),
    )

    assert first.model_dump_json() == second.model_dump_json()
    assert hidden.document_id not in first.document_ids
    assert first.scope_expanding is False
    assert first.completeness == NeighborhoodCompleteness.BOUNDED_NOT_EXHAUSTIVE
    assert all(association.scope_expanding is False for association in first.associations)
