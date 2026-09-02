"""Offline contracts for document investigation and association semantics v1."""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from models.document_investigation import (
    AssociationDimension,
    AssociationStrength,
    CalendarBasis,
    CalendarGranularity,
    CalendarMatchStatus,
    InvestigationConflictCode,
    InvestigationStatus,
    InvestigationTimezoneStatus,
    NeighborhoodCompleteness,
    NeighborhoodLimits,
)
from models.document_metadata import (
    DocumentMetadataProfile,
    MetadataConflict,
    MetadataNormalizationStatus,
    MetadataObservation,
    MetadataSectionName,
    MetadataSourceType,
    MetadataTrustClass,
)
from models.source_provenance import (
    SourceEntity,
    SourceProvenance,
    SourceRelation,
    SourceRelationRole,
)
from services.document_investigation import (
    build_candidate_lineage_evidence,
    build_document_association,
    build_document_chronology,
    build_documentary_neighborhood,
    compare_document_metadata,
    inspect_document_metadata,
    project_association_evidence,
    project_document_metadata_evidence,
)
from services.scope_traversal_policy import ScopeRelationSemantics, ScopeTraversalPolicy

_EXTRACTED_AT = datetime(2026, 9, 2, 10, 0, tzinfo=UTC)
_FIXTURE_PATH = (
    Path(__file__).parents[2]
    / "fixtures"
    / "document_investigation"
    / "association-cases-v1.json"
)
_TIME_FIELDS = {
    "embedded_created_at",
    "embedded_modified_at",
    "embedded_sent_at",
    "embedded_digitized_at",
    "filesystem_birthtime",
    "filesystem_mtime",
    "filesystem_ctime",
    "archived_at",
    "archive_created_at",
    "archive_modified_at",
    "ingested_at",
}


def _timezone_label(parsed: datetime) -> str:
    offset = parsed.utcoffset()
    if offset is None:
        return "UNKNOWN"
    if offset.total_seconds() == 0:
        return "Z"
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    return f"{sign}{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def _observation(
    field: str,
    value: Any,
    *,
    source: str = "synthetic_fixture",
    section: MetadataSectionName | None = None,
    extracted_at: datetime = _EXTRACTED_AT,
) -> MetadataObservation:
    if section is None:
        if field in {"original_filename", "extension", "mime_type", "sha256"}:
            section = MetadataSectionName.IDENTITY
        elif field.startswith("archive_") or field in {"archived_at", "source_entity_family"}:
            section = MetadataSectionName.ARCHIVE
        elif field.startswith("filesystem_"):
            section = MetadataSectionName.FILESYSTEM
        elif field == "ingested_at":
            section = MetadataSectionName.INGESTION
        else:
            section = MetadataSectionName.EMBEDDED
    if field in _TIME_FIELDS:
        raw_value = value
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            normalized_value = None
            timezone = "UNKNOWN"
            status = MetadataNormalizationStatus.INVALID
        else:
            normalized_value = str(value).replace("Z", "+00:00")
            timezone = _timezone_label(parsed)
            status = (
                MetadataNormalizationStatus.TIMEZONE_EXPLICIT
                if parsed.tzinfo is not None
                else MetadataNormalizationStatus.TIMEZONE_UNKNOWN
            )
        return MetadataObservation(
            section=section,
            field=field,
            value=normalized_value,
            raw_value=raw_value,
            source=source,
            source_type=MetadataSourceType.FORMAT_NATIVE,
            trust_class=MetadataTrustClass.EMBEDDED_DOCUMENT_METADATA,
            extracted_at=extracted_at,
            normalization_status=status,
            timezone=timezone,
        )
    return MetadataObservation(
        section=section,
        field=field,
        value=value,
        source=source,
        source_type=(
            MetadataSourceType.ARCHIVE_NATIVE
            if section == MetadataSectionName.ARCHIVE
            else MetadataSourceType.FORMAT_NATIVE
        ),
        trust_class=(
            MetadataTrustClass.ARCHIVE_SYSTEM
            if section == MetadataSectionName.ARCHIVE
            else MetadataTrustClass.EMBEDDED_DOCUMENT_METADATA
        ),
        extracted_at=extracted_at,
        normalization_status=MetadataNormalizationStatus.NORMALIZED,
    )


def _profile(
    document_id: str,
    values: dict[str, Any] | None = None,
    *,
    observations: list[MetadataObservation] | None = None,
    conflicts: list[MetadataConflict] | None = None,
    extracted_at: datetime = _EXTRACTED_AT,
) -> DocumentMetadataProfile:
    values = values or {}
    items = list(observations or [])
    field_map = {
        "created": "embedded_created_at",
        "modified": "embedded_modified_at",
        "archived": "archived_at",
        "mime": "mime_type",
        "filename": "original_filename",
        "extension": "extension",
        "creator": values.get("creator_field", "creator"),
        "last_modifier": "last_modified_by",
        "producer": "producer",
        "creator_application": "creator_application",
        "sha256": "sha256",
        "documentary_type": "documentary_type",
        "source_family": "source_entity_family",
    }
    for fixture_name, field_name in field_map.items():
        if fixture_name in values:
            source = str(values.get(f"{fixture_name}_source", "synthetic_fixture"))
            items.append(
                _observation(
                    str(field_name),
                    values[fixture_name],
                    source=source,
                    extracted_at=extracted_at,
                )
            )
    sections: dict[MetadataSectionName, list[MetadataObservation]] = {
        section: [] for section in MetadataSectionName
    }
    for item in items:
        sections[item.section].append(item)
    return DocumentMetadataProfile(
        entity_id=document_id,
        identity=sections[MetadataSectionName.IDENTITY],
        embedded=sections[MetadataSectionName.EMBEDDED],
        filesystem=sections[MetadataSectionName.FILESYSTEM],
        archive=sections[MetadataSectionName.ARCHIVE],
        ingestion=sections[MetadataSectionName.INGESTION],
        conflicts=conflicts or [],
    )


def _provenance(document_id: str, values: dict[str, Any]) -> SourceProvenance | None:
    source_system = values.get("source_system")
    parent_email = values.get("parent_email")
    if source_system is None and parent_email is None:
        return None
    relations = []
    if parent_email:
        relations.append(
            SourceRelation(
                role=SourceRelationRole.ATTACHMENT_OF,
                target=SourceEntity(id=parent_email, type="email_message"),
            )
        )
    return SourceProvenance(
        entity=SourceEntity(
            id=document_id,
            type="email_attachment" if parent_email else "file",
            source_system=source_system,
        ),
        relations=relations,
    )


def _inspection(document_id: str, values: dict[str, Any]):
    return inspect_document_metadata(
        _profile(document_id, values),
        source_provenance=_provenance(document_id, values),
    )


def _association(
    left_values: dict[str, Any],
    right_values: dict[str, Any],
    *,
    suffix: str = "pair",
):
    left = _inspection(f"urn:test:{suffix}:left", left_values)
    right = _inspection(f"urn:test:{suffix}:right", right_values)
    return build_document_association(left, right)


def _fixture_cases() -> list[dict[str, Any]]:
    fixture = json.loads(_FIXTURE_PATH.read_text())
    assert fixture["schema"] == "openrag.document-investigation.synthetic-associations"
    assert fixture["version"] == 1
    return fixture["cases"]


@pytest.mark.parametrize("case", _fixture_cases(), ids=lambda case: case["name"])
def test_synthetic_association_dimension_matrix(case: dict[str, Any]):
    association = _association(case["left"], case["right"], suffix=case["name"])
    dimensions = {item.value for item in association.dimensions}

    assert set(case["expected_contains"]) <= dimensions
    assert not (set(case["expected_excludes"]) & dimensions)
    assert association.association_strength.value == case["strength"]
    assert association.scope_expanding is False


def test_complete_v1_dimension_taxonomy_has_direct_fixture_coverage():
    values = {
        "created": "2024-03-02T08:00:00Z",
        "modified": "2024-04-03T09:00:00Z",
        "source_system": "archive-a",
        "source_family": "occurrence-family-1",
        "parent_email": "urn:test:mail:dimension-coverage",
        "mime": "application/pdf",
        "filename": "report.pdf",
        "extension": ".pdf",
        "creator": "Alice",
        "last_modifier": "Bob",
        "producer": "PDF Producer",
        "creator_application": "PDF Tool",
        "sha256": "d" * 64,
        "documentary_type": "source-declared-report",
    }

    association = _association(values, values, suffix="dimension-coverage")

    assert set(association.dimensions) == set(AssociationDimension)


def _calendar_result(comparison, basis, granularity):
    matches = [
        item
        for item in comparison.calendar_period_associations
        if item.basis == basis and item.granularity == granularity
    ]
    assert len(matches) == 1
    return matches[0]


def test_timezone_unknown_keeps_local_calendar_but_instant_and_utc_are_indeterminate():
    left = _inspection("urn:test:naive", {"created": "2024-03-02T08:00:00"})
    right = _inspection("urn:test:aware", {"created": "2024-03-27T18:00:00Z"})

    comparison = compare_document_metadata(left, right)

    assert comparison.temporal_relations[0].relation.value == "INDETERMINATE"
    assert (
        _calendar_result(comparison, CalendarBasis.SOURCE_LOCAL, CalendarGranularity.MONTH).status
        == CalendarMatchStatus.MATCH
    )
    assert (
        _calendar_result(comparison, CalendarBasis.UTC, CalendarGranularity.MONTH).status
        == CalendarMatchStatus.INDETERMINATE
    )
    assert AssociationDimension.SAME_PRODUCTION_MONTH in comparison.dimensions
    assert AssociationDimension.SAME_PRODUCTION_MONTH_UTC not in comparison.dimensions
    assert left.temporal_observations[0].timezone_status == InvestigationTimezoneStatus.UNKNOWN


def test_same_instant_exposes_different_local_month_and_same_utc_month():
    left = _inspection("urn:test:march", {"created": "2024-03-31T23:30:00Z"})
    right = _inspection("urn:test:april", {"created": "2024-04-01T01:30:00+02:00"})

    comparison = compare_document_metadata(left, right)

    assert comparison.temporal_relations[0].relation.value == "EQUAL"
    assert (
        _calendar_result(comparison, CalendarBasis.SOURCE_LOCAL, CalendarGranularity.MONTH).status
        == CalendarMatchStatus.DIFFERENT
    )
    assert (
        _calendar_result(comparison, CalendarBasis.UTC, CalendarGranularity.MONTH).status
        == CalendarMatchStatus.MATCH
    )
    assert AssociationDimension.SAME_PRODUCTION_INSTANT in comparison.dimensions
    assert AssociationDimension.SAME_PRODUCTION_MONTH not in comparison.dimensions
    assert AssociationDimension.SAME_PRODUCTION_MONTH_UTC in comparison.dimensions


def test_same_instant_exposes_year_boundary_without_silent_calendar_choice():
    association = _association(
        {"created": "2023-12-31T23:30:00Z"},
        {"created": "2024-01-01T01:30:00+02:00"},
        suffix="year-boundary",
    )

    assert AssociationDimension.SAME_PRODUCTION_INSTANT in association.dimensions
    assert AssociationDimension.SAME_PRODUCTION_YEAR not in association.dimensions
    assert AssociationDimension.SAME_PRODUCTION_YEAR_UTC in association.dimensions


def test_invalid_future_and_unknown_dates_remain_explicit_conflicts():
    invalid = inspect_document_metadata(
        _profile("urn:test:invalid", {"created": "2024-99-99"})
    )
    future = inspect_document_metadata(
        _profile("urn:test:future", {"created": "2099-01-01T00:00:00Z"})
    )

    assert invalid.temporal_observations[0].raw_value == "2024-99-99"
    assert invalid.temporal_observations[0].normalized_value is None
    assert invalid.temporal_observations[0].status == InvestigationStatus.INVALID
    assert InvestigationConflictCode.INVALID_TIMESTAMP in {item.code for item in invalid.conflicts}
    assert InvestigationConflictCode.FUTURE_TIMESTAMP in {item.code for item in future.conflicts}


def test_pdf_info_xmp_conflict_and_multiple_creation_observations_are_preserved():
    observations = [
        _observation(
            "embedded_created_at",
            "2024-03-01T10:00:00Z",
            source="pdf_info_dictionary",
        ),
        _observation(
            "embedded_created_at",
            "2024-04-01T10:00:00Z",
            source="pdf_xmp",
        ),
    ]
    profile = _profile(
        "urn:test:pdf-conflict",
        observations=observations,
        conflicts=[
            MetadataConflict(
                field="embedded_created_at",
                values=["2024-03-01T10:00:00+00:00", "2024-04-01T10:00:00+00:00"],
                sources=["pdf_info_dictionary", "pdf_xmp"],
            )
        ],
    )

    inspection = inspect_document_metadata(profile)
    codes = {item.code for item in inspection.conflicts}

    assert len(inspection.temporal_observations) == 2
    assert {item.source for item in inspection.temporal_observations} == {
        "pdf_info_dictionary",
        "pdf_xmp",
    }
    assert InvestigationConflictCode.SOURCE_CONFLICT in codes
    assert InvestigationConflictCode.MULTIPLE_CREATION_OBSERVATIONS in codes


def test_temporal_association_evidence_retains_both_fields_sources_and_period():
    left = _inspection(
        "urn:test:evidence:left",
        {
            "created": "2024-03-02T08:00:00Z",
            "created_source": "pdf_xmp",
        },
    )
    right = _inspection(
        "urn:test:evidence:right",
        {
            "created": "2024-03-27T18:00:00Z",
            "created_source": "ooxml_core_properties",
        },
    )

    association = build_document_association(left, right)
    evidence = next(
        item
        for item in association.evidence
        if item.dimension == AssociationDimension.SAME_PRODUCTION_MONTH
    )

    assert evidence.left_field == "embedded_created_at"
    assert evidence.right_field == "embedded_created_at"
    assert evidence.left_source == "pdf_xmp"
    assert evidence.right_source == "ooxml_core_properties"
    assert evidence.calendar_basis == CalendarBasis.SOURCE_LOCAL
    assert evidence.normalized_period == "2024-03"


def test_partial_observation_overlap_is_reported_as_conflicting_not_preferred_truth():
    left = inspect_document_metadata(
        _profile(
            "urn:test:overlap:left",
            observations=[
                _observation(
                    "embedded_created_at",
                    "2024-03-01T00:00:00Z",
                    source="pdf_info_dictionary",
                ),
                _observation(
                    "embedded_created_at",
                    "2024-04-01T00:00:00Z",
                    source="pdf_xmp",
                ),
            ],
        )
    )
    right = _inspection(
        "urn:test:overlap:right",
        {"created": "2024-03-15T00:00:00Z"},
    )

    association = build_document_association(left, right)
    month = next(
        item
        for item in association.dimension_results
        if item.dimension == AssociationDimension.SAME_PRODUCTION_MONTH
    )

    assert month.status == InvestigationStatus.CONFLICTING
    assert association.association_status == InvestigationStatus.CONFLICTING
    assert len(left.temporal_observations) == 2


def test_modified_before_created_and_archive_inversion_are_not_resolved():
    inspection = inspect_document_metadata(
        _profile(
            "urn:test:inversion",
            {
                "created": "2024-05-01T00:00:00Z",
                "modified": "2024-04-01T00:00:00Z",
                "archived": "2024-03-01T00:00:00Z",
            },
        )
    )

    codes = {item.code for item in inspection.conflicts}
    assert InvestigationConflictCode.MODIFIED_BEFORE_CREATED in codes
    assert InvestigationConflictCode.ARCHIVE_EMBEDDED_DATE_INVERSION in codes


def test_creator_change_is_observed_but_names_are_not_globally_resolved():
    inspection = inspect_document_metadata(
        _profile(
            "urn:test:creator-change",
            {"creator": "Jean Dupont", "last_modifier": "J. Dupont"},
        )
    )
    association = _association(
        {"creator": "Jean Dupont"},
        {"creator": "J. Dupont"},
        suffix="creator-identity",
    )

    assert InvestigationConflictCode.CREATOR_CHANGED in {
        item.code for item in inspection.conflicts
    }
    assert AssociationDimension.SAME_CREATOR_OBSERVATION not in association.dimensions


def test_cross_format_family_is_compatible_without_inventing_documentary_type():
    association = _association(
        {"mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
        {"mime": "application/vnd.oasis.opendocument.text"},
        suffix="cross-format",
    )

    assert AssociationDimension.COMPATIBLE_DOCUMENT_TYPES in association.dimensions
    assert AssociationDimension.SAME_MIME_TYPE not in association.dimensions
    assert AssociationDimension.SAME_DOCUMENT_TYPE not in association.dimensions


def test_source_declared_document_type_is_separate_from_mime_and_extension():
    association = _association(
        {"mime": "application/pdf", "documentary_type": "invoice"},
        {"mime": "application/pdf", "documentary_type": "technical report"},
        suffix="type-separation",
    )

    assert AssociationDimension.SAME_MIME_TYPE in association.dimensions
    assert AssociationDimension.SAME_DOCUMENT_TYPE not in association.dimensions


def test_same_binary_preserves_distinct_occurrence_endpoints():
    digest = "c" * 64
    association = _association(
        {"sha256": digest, "source_system": "archive-a"},
        {"sha256": digest, "source_system": "archive-b"},
        suffix="same-binary",
    )

    assert association.left_document_id != association.right_document_id
    assert association.association_strength == AssociationStrength.STRONG
    assert AssociationDimension.SAME_BINARY_HASH in association.dimensions
    assert AssociationDimension.SAME_SOURCE_SYSTEM not in association.dimensions


def test_same_source_different_parent_emails_has_no_shared_parent_association():
    association = _association(
        {"source_system": "openarchiver", "parent_email": "urn:test:mail:1"},
        {"source_system": "openarchiver", "parent_email": "urn:test:mail:2"},
        suffix="unrelated-emails",
    )

    assert AssociationDimension.SAME_SOURCE_SYSTEM in association.dimensions
    assert AssociationDimension.SAME_PARENT_COLLECTION not in association.dimensions
    assert association.association_strength == AssociationStrength.VERY_WEAK


def test_same_filename_with_different_attachment_parents_remains_weak():
    association = _association(
        {
            "source_system": "openarchiver",
            "parent_email": "urn:test:mail:1",
            "filename": "facture.pdf",
        },
        {
            "source_system": "openarchiver",
            "parent_email": "urn:test:mail:2",
            "filename": "facture.pdf",
        },
        suffix="filename-different-attachments",
    )

    assert AssociationDimension.SAME_FILENAME_BASENAME in association.dimensions
    assert AssociationDimension.SAME_PARENT_COLLECTION not in association.dimensions
    assert association.association_strength == AssociationStrength.WEAK


def test_shared_parent_is_association_evidence_not_a_new_provenance_edge():
    left = _inspection(
        "urn:test:attachment:1",
        {"source_system": "openarchiver", "parent_email": "urn:test:mail:42"},
    )
    right = _inspection(
        "urn:test:attachment:2",
        {"source_system": "openarchiver", "parent_email": "urn:test:mail:42"},
    )
    association = build_document_association(left, right)
    lineage = build_candidate_lineage_evidence(association)

    assert AssociationDimension.SAME_PARENT_COLLECTION in association.dimensions
    assert left.safe_provenance.asserted_relations[0].role == "attachment_of"
    assert lineage.status == "candidate_only"
    assert lineage.scope_expanding is False
    assert lineage.prov_o_edges == 0


def test_association_role_is_unknown_and_non_traversable_under_existing_scope_policy():
    decision = ScopeTraversalPolicy().classify(
        role="associated_with",
        source_type="file",
        target_type="file",
    )

    assert decision.follow_forward is False
    assert decision.follow_reverse is False
    assert decision.certifiable is False
    assert decision.semantics == ScopeRelationSemantics.UNCLASSIFIED


def test_chronology_preserves_comparable_and_indeterminate_partial_order():
    profile = _profile(
        "urn:test:chronology",
        observations=[
            _observation("embedded_created_at", "2024-03-01T00:00:00Z"),
            _observation("embedded_modified_at", "2024-03-02T00:00:00"),
            _observation("archived_at", "2024-03-03T00:00:00Z"),
        ],
    )

    chronology = build_document_chronology(profile)

    assert chronology.comparable_relations
    assert chronology.indeterminate_relations
    assert any(
        item.code == InvestigationConflictCode.TIMEZONE_UNKNOWN
        for item in chronology.conflicts
    )
    assert chronology.provenance.document_id == profile.entity_id


def test_association_is_symmetric_canonical_and_input_order_invariant():
    left_profile = _profile(
        "urn:test:canonical:a",
        {
            "created": "2024-03-02T08:00:00Z",
            "creator": "Alice",
            "mime": "application/pdf",
        },
    )
    right_profile = _profile(
        "urn:test:canonical:b",
        {
            "created": "2024-03-27T18:00:00Z",
            "creator": "Alice",
            "mime": "application/pdf",
        },
    )
    left = inspect_document_metadata(left_profile)
    right = inspect_document_metadata(right_profile)

    forward = build_document_association(left, right)
    reverse = build_document_association(right, left)

    assert forward.canonical_json() == reverse.canonical_json()
    assert forward.document_association_sha256 == reverse.document_association_sha256
    assert forward.model_dump(mode="json") == reverse.model_dump(mode="json")


def test_metadata_observation_permutation_and_extraction_time_do_not_change_digest():
    observations = [
        _observation("creator", "Alice", extracted_at=datetime(2026, 1, 1, tzinfo=UTC)),
        _observation(
            "embedded_created_at",
            "2024-03-02T08:00:00Z",
            extracted_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    ]
    later = [
        item.model_copy(update={"extracted_at": datetime(2026, 8, 1, tzinfo=UTC)})
        for item in reversed(observations)
    ]
    left_first = inspect_document_metadata(_profile("urn:test:stable:a", observations=observations))
    left_later = inspect_document_metadata(_profile("urn:test:stable:a", observations=later))
    right = _inspection(
        "urn:test:stable:b",
        {"creator": "Alice", "created": "2024-03-27T18:00:00Z"},
    )

    first = build_document_association(left_first, right)
    second = build_document_association(left_later, right)

    assert first.canonical_json() == second.canonical_json()
    assert first.document_association_sha256 == second.document_association_sha256


def test_candidate_keys_hash_sensitive_creator_source_and_parent_values():
    inspection = _inspection(
        "urn:test:opaque-keys",
        {
            "creator": "Sensitive Person",
            "source_system": "Private Archive",
            "parent_email": "urn:private:mail:secret",
        },
    )
    serialized = json.dumps(
        [item.model_dump(mode="json") for item in inspection.association_keys]
    ).casefold()

    assert "sensitive person" not in serialized
    assert "private archive" not in serialized
    assert "urn:private:mail:secret" not in serialized
    assert all(len(item.value_sha256) == 64 for item in inspection.association_keys)


def test_mega_hub_buckets_are_capped_and_never_scope_expanding():
    inspections = [
        _inspection(f"urn:test:mega:{index:03d}", {"source_system": "mega-archive"})
        for index in range(30)
    ]
    neighborhood = build_documentary_neighborhood(
        ["urn:test:mega:000"],
        inspections,
        accessible_document_ids={item.document_id for item in inspections},
        limits=NeighborhoodLimits(
            max_documents=4,
            max_associations=3,
            per_dimension_limit=3,
        ),
    )

    assert len(neighborhood.document_ids) <= 4
    assert len(neighborhood.associations) <= 3
    assert AssociationDimension.SAME_SOURCE_SYSTEM in neighborhood.truncated_dimensions
    assert neighborhood.completeness == NeighborhoodCompleteness.BOUNDED_NOT_EXHAUSTIVE
    assert neighborhood.scope_expanding is False
    assert all(item.scope_expanding is False for item in neighborhood.associations)


def test_per_dimension_limit_applies_across_conflicting_value_buckets():
    seed = inspect_document_metadata(
        _profile(
            "urn:test:multi-bucket:seed",
            observations=[
                _observation("creator", "Alice", source="pdf_info_dictionary"),
                _observation("creator", "Bob", source="pdf_xmp"),
            ],
        )
    )
    candidates = [
        _inspection(f"urn:test:multi-bucket:{name}", {"creator": creator})
        for name, creator in (
            ("alice-1", "Alice"),
            ("alice-2", "Alice"),
            ("bob-1", "Bob"),
            ("bob-2", "Bob"),
        )
    ]
    inspections = [seed, *candidates]

    neighborhood = build_documentary_neighborhood(
        [seed.document_id],
        inspections,
        accessible_document_ids={item.document_id for item in inspections},
        dimensions={AssociationDimension.SAME_CREATOR_OBSERVATION},
        limits=NeighborhoodLimits(per_dimension_limit=2),
    )

    assert len(neighborhood.associations) == 2
    assert AssociationDimension.SAME_CREATOR_OBSERVATION in neighborhood.truncated_dimensions


@pytest.mark.parametrize(
    "dimension,values",
    [
        (AssociationDimension.SAME_PRODUCTION_YEAR, {"created": "2024-01-01T00:00:00Z"}),
        (AssociationDimension.SAME_SOURCE_SYSTEM, {"source_system": "mega-source"}),
        (AssociationDimension.SAME_MIME_TYPE, {"mime": "application/pdf"}),
    ],
)
def test_adversarial_mega_hub_dimensions_alone_are_non_expanding(dimension, values):
    association = _association(values, values, suffix=dimension.value.casefold())

    assert dimension in association.dimensions
    assert association.scope_expanding is False
    assert association.association_strength in {
        AssociationStrength.VERY_WEAK,
        AssociationStrength.WEAK,
    }


def test_dls_filters_hidden_documents_before_bucketing_or_projection():
    seed = _inspection("urn:test:dls:seed", {"source_system": "archive"})
    visible = _inspection("urn:test:dls:visible", {"source_system": "archive"})
    hidden = _inspection(
        "urn:test:dls:hidden",
        {"source_system": "archive", "creator": "Hidden Person"},
    )
    neighborhood = build_documentary_neighborhood(
        [seed.document_id],
        [hidden, visible, seed],
        accessible_document_ids={seed.document_id, visible.document_id},
        limits=NeighborhoodLimits(per_dimension_limit=1),
    )
    serialized = neighborhood.model_dump_json()
    hidden_association = build_document_association(seed, hidden)

    assert hidden.document_id not in serialized
    assert "Hidden Person" not in serialized
    assert neighborhood.truncated_dimensions == []
    assert (
        project_association_evidence(
            hidden_association,
            accessible_document_ids={seed.document_id},
        )
        is None
    )
    assert (
        project_document_metadata_evidence(
            hidden,
            accessible_document_ids={seed.document_id},
        )
        is None
    )


def test_evidence_projection_is_bounded_stable_and_hides_raw_actor_value():
    association = _association(
        {
            "created": "2024-03-02T08:00:00Z",
            "creator": "Sensitive Person",
            "source_system": "archive",
            "mime": "application/pdf",
        },
        {
            "created": "2024-03-27T18:00:00Z",
            "creator": "Sensitive Person",
            "source_system": "archive",
            "mime": "application/pdf",
        },
        suffix="projection",
    )
    allowed = {association.left_document_id, association.right_document_id}

    projection = project_association_evidence(
        association,
        accessible_document_ids=allowed,
        max_dimensions=2,
    )

    assert projection is not None
    assert len(projection.reasons) == 2
    assert projection.truncated is True
    assert projection.scope_expanding is False
    assert "Sensitive Person" not in projection.model_dump_json()


def test_neighborhood_is_identical_for_bucket_input_permutations():
    inspections = [
        _inspection(
            f"urn:test:ordered:{index}",
            {
                "source_system": "archive",
                "created": f"2024-03-{index + 1:02d}T00:00:00Z",
            },
        )
        for index in range(6)
    ]
    allowed = {item.document_id for item in inspections}
    limits = NeighborhoodLimits(max_documents=5, max_associations=4, per_dimension_limit=4)

    forward = build_documentary_neighborhood(
        [inspections[0].document_id],
        inspections,
        accessible_document_ids=allowed,
        limits=limits,
    )
    reverse = build_documentary_neighborhood(
        [inspections[0].document_id],
        list(reversed(inspections)),
        accessible_document_ids=allowed,
        limits=limits,
    )

    assert forward.model_dump_json() == reverse.model_dump_json()


def test_neighborhood_time_window_and_source_scope_are_explicit_bounds():
    seed = _inspection(
        "urn:test:bounds:seed",
        {"source_system": "archive-a", "created": "2024-03-01T00:00:00Z"},
    )
    near = _inspection(
        "urn:test:bounds:near",
        {"source_system": "archive-a", "created": "2024-03-02T00:00:00Z"},
    )
    far = _inspection(
        "urn:test:bounds:far",
        {"source_system": "archive-a", "created": "2024-03-20T00:00:00Z"},
    )
    other_source = _inspection(
        "urn:test:bounds:other",
        {"source_system": "archive-b", "created": "2024-03-02T00:00:00Z"},
    )
    allowed = {item.document_id for item in (seed, near, far, other_source)}

    neighborhood = build_documentary_neighborhood(
        [seed.document_id],
        [far, other_source, near, seed],
        accessible_document_ids=allowed,
        limits=NeighborhoodLimits(time_window_days=3, source_scope=("archive-a",)),
    )

    assert near.document_id in neighborhood.document_ids
    assert far.document_id not in neighborhood.document_ids
    assert other_source.document_id not in neighborhood.document_ids


def test_module_has_no_runtime_integration_dependencies_or_domain_logic():
    root = Path(__file__).parents[3]
    paths = [
        root / "src" / "models" / "document_investigation.py",
        root / "src" / "services" / "document_investigation.py",
    ]
    forbidden_import_roots = {"opensearchpy", "api", "connectors", "flows"}
    forbidden_domain_terms = {
        "surface pastorale",
        "orange",
        "pommerieux",
        "agriculture",
        "legal case",
        "network outage",
    }

    for path in paths:
        source = path.read_text()
        tree = ast.parse(source)
        imports = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        assert not (imports & forbidden_import_roots)
        assert not {term for term in forbidden_domain_terms if term in source.casefold()}
