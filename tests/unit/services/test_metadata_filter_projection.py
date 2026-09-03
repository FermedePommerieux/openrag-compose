"""Projection, query, evidence, DLS-boundary, and canary tests."""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import product
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from models.document_investigation import CalendarBasis
from models.document_metadata import (
    DocumentMetadataProfile,
    MetadataConflict,
    MetadataNormalizationStatus,
    MetadataObservation,
    MetadataSectionName,
    MetadataSourceType,
    MetadataTrustClass,
    document_metadata_mapping,
)
from models.metadata_filter import (
    MetadataDateSourcePolicy,
    MetadataFilter,
    MetadataFilterBooleanOperator,
    MetadataFilterClause,
    MetadataFilterExpression,
    MetadataFilterField,
    MetadataFilterOperator,
    MetadataTruthValue,
)
from models.metadata_filter_projection import (
    MetadataFilterProjection,
    MetadataFilterProjectionSourceContext,
    metadata_filter_projection_mapping,
)
from models.source_provenance import (
    SourceEntity,
    SourceProvenance,
    SourceRelation,
    SourceRelationRole,
)
from services.metadata_filter import truth_and, truth_not, truth_or
from services.metadata_filter_projection import (
    MetadataProjectionQueryBoundary,
    build_projection_side_document,
    compile_metadata_filter_to_opensearch,
    evaluate_metadata_filter_projection,
    generate_metadata_filter_projection,
    safe_parent_collection_id,
)
from services.metadata_filter_projection_canary import MetadataFilterProjectionCanary

_EXTRACTED = datetime(2026, 9, 3, tzinfo=UTC)
_ENTITY = "urn:test:document:one"


def _observation(
    section: MetadataSectionName,
    field: str,
    value: str | None,
    *,
    source: str,
    status: MetadataNormalizationStatus = MetadataNormalizationStatus.NORMALIZED,
    raw_value: str | None = None,
    timezone: str | None = None,
) -> MetadataObservation:
    return MetadataObservation(
        section=section,
        field=field,
        value=value,
        raw_value=raw_value,
        source=source,
        source_type=(
            MetadataSourceType.FORMAT_NATIVE
            if section in {MetadataSectionName.IDENTITY, MetadataSectionName.EMBEDDED}
            else MetadataSourceType.INGESTION
        ),
        trust_class=(
            MetadataTrustClass.EMBEDDED_DOCUMENT_METADATA
            if section in {MetadataSectionName.IDENTITY, MetadataSectionName.EMBEDDED}
            else MetadataTrustClass.INGESTION_SYSTEM
        ),
        extracted_at=_EXTRACTED,
        normalization_status=status,
        timezone=timezone,
    )


def _fixture() -> tuple[
    DocumentMetadataProfile,
    MetadataFilterProjectionSourceContext,
    SourceProvenance,
]:
    identity = [
        _observation(
            MetadataSectionName.IDENTITY,
            "mime_type",
            "application/pdf",
            source="binary_identity",
        ),
        _observation(
            MetadataSectionName.IDENTITY,
            "extension",
            ".PDF",
            source="binary_identity",
        ),
        _observation(
            MetadataSectionName.IDENTITY,
            "original_filename",
            "  Rapport  Final.PDF",
            source="binary_identity",
        ),
        _observation(
            MetadataSectionName.IDENTITY,
            "sha256",
            "a" * 64,
            source="binary_identity",
        ),
    ]
    embedded = [
        _observation(
            MetadataSectionName.EMBEDDED,
            "embedded_created_at",
            "2024-03-31T23:30:00-02:00",
            raw_value="D:20240331233000-02'00'",
            source="pdf_info_dictionary",
            status=MetadataNormalizationStatus.TIMEZONE_EXPLICIT,
            timezone="-02:00",
        ),
        _observation(
            MetadataSectionName.EMBEDDED,
            "embedded_created_at",
            "2024-04-02T00:00:00+00:00",
            raw_value="2024-04-02T00:00:00Z",
            source="pdf_xmp",
            status=MetadataNormalizationStatus.TIMEZONE_EXPLICIT,
            timezone="Z",
        ),
        _observation(
            MetadataSectionName.EMBEDDED,
            "embedded_modified_at",
            "2023-07-01T12:00:00",
            raw_value="2023-07-01T12:00:00",
            source="pdf_info_dictionary",
            status=MetadataNormalizationStatus.TIMEZONE_UNKNOWN,
            timezone="UNKNOWN",
        ),
        _observation(
            MetadataSectionName.EMBEDDED,
            "creator",
            " Alice\u3000 Smith ",
            source="pdf_info_dictionary",
        ),
        _observation(
            MetadataSectionName.EMBEDDED,
            "creator",
            "alice smith",
            source="pdf_xmp",
        ),
        _observation(
            MetadataSectionName.EMBEDDED,
            "producer",
            " LibreOffice  7 ",
            source="pdf_info_dictionary",
        ),
        _observation(
            MetadataSectionName.EMBEDDED,
            "documentary_type",
            "Invoice",
            source="pdf_xmp",
        ),
    ]
    profile = DocumentMetadataProfile(
        entity_id=_ENTITY,
        identity=identity,
        embedded=embedded,
        conflicts=[
            MetadataConflict(
                field="embedded_created_at",
                values=["2024-03-31T23:30:00-02:00", "2024-04-02T00:00:00+00:00"],
                sources=["pdf_info_dictionary", "pdf_xmp"],
            )
        ],
    )
    provenance = SourceProvenance(
        entity=SourceEntity(
            id=_ENTITY,
            type="email_attachment",
            source_system="OpenArchiver",
        ),
        relations=[
            SourceRelation(
                role=SourceRelationRole.ATTACHMENT_OF,
                target=SourceEntity(id="private-parent-42", type="email_message"),
            )
        ],
    )
    context = MetadataFilterProjectionSourceContext(
        source_entity_id=_ENTITY,
        source_entity_type="email_attachment",
        source_system="OpenArchiver",
        connector="OpenArchiver",
        mime_type="application/pdf; charset=binary",
        filename="Rapport Final.PDF",
    )
    return profile, context, provenance


def _projection() -> MetadataFilterProjection:
    profile, context, provenance = _fixture()
    return generate_metadata_filter_projection(
        profile,
        source_context=context,
        source_provenance=provenance,
    )


def _temporal_clause(
    value: str,
    *,
    negated: bool = False,
    explicit_source: str | None = None,
) -> MetadataFilterClause:
    return MetadataFilterClause(
        field=MetadataFilterField.PRODUCTION_MONTH,
        operator=MetadataFilterOperator.EQUAL,
        values=(value,),
        calendar_basis=CalendarBasis.SOURCE_LOCAL,
        source_policy=(
            MetadataDateSourcePolicy.EXPLICIT_SOURCE
            if explicit_source
            else MetadataDateSourcePolicy.ANY_VALID_PRODUCTION_OBSERVATION
        ),
        explicit_source=explicit_source,
        negated=negated,
    )


def test_projection_is_multivalued_deterministic_and_does_not_collapse_truth():
    profile, _, _ = _fixture()
    projection = _projection()

    assert projection.source_metadata_facts_sha256 == profile.metadata_facts_sha256
    assert projection.production_month_local == ("2024-03", "2024-04")
    assert projection.production_month_utc == ("2024-04",)
    assert projection.modification_month_local == ("2023-07",)
    assert projection.modification_month_utc == ()
    assert projection.creator_normalized == ("alice smith",)
    assert projection.producer_normalized == ("libreoffice 7",)
    assert projection.explicit_document_types == ("invoice",)
    assert projection.source_entity_types == ("email_attachment",)
    assert projection.source_entity_families == ()
    assert projection.has_temporal_conflict is True
    assert projection.has_metadata_conflict is True
    assert projection.has_timezone_unknown is True
    assert projection.projection_sha256 == projection.calculate_sha256()


def test_projection_hides_parent_locator_and_keeps_raw_profile_unchanged():
    profile, _, _ = _fixture()
    before = profile.model_dump_json()
    projection = _projection()

    assert projection.parent_collection_ids_safe == (
        safe_parent_collection_id("private-parent-42"),
    )
    assert "private-parent-42" not in projection.model_dump_json()
    assert profile.model_dump_json() == before
    assert document_metadata_mapping()["document_metadata_profile"] == {
        "type": "object",
        "enabled": False,
    }


def test_projection_digest_is_stable_and_detects_context_change():
    first = _projection()
    second = _projection()
    assert first.canonical_json() == second.canonical_json()
    assert first.projection_sha256 == second.projection_sha256

    profile, context, provenance = _fixture()
    changed = generate_metadata_filter_projection(
        profile,
        source_context=context.model_copy(update={"connector": "other connector"}),
        source_provenance=provenance,
    )
    assert changed.source_metadata_facts_sha256 == first.source_metadata_facts_sha256
    assert changed.source_context_sha256 != first.source_context_sha256
    assert changed.projection_sha256 != first.projection_sha256


def test_projection_rejects_digest_mismatch_and_identity_mismatch():
    projection = _projection()
    with pytest.raises(ValidationError, match="projection_sha256"):
        MetadataFilterProjection.model_validate(
            {**projection.model_dump(mode="json"), "projection_sha256": "0" * 64}
        )
    profile, context, provenance = _fixture()
    with pytest.raises(ValueError, match="context identity"):
        generate_metadata_filter_projection(
            profile,
            source_context=context.model_copy(update={"source_entity_id": "other"}),
            source_provenance=provenance,
        )


def test_mapping_is_strict_exact_and_multivalued_compatible():
    mapping = metadata_filter_projection_mapping()
    assert mapping["dynamic"] == "strict"
    properties = mapping["properties"]["filter"]["properties"]
    assert properties["production_day_local"]["type"] == "date"
    assert properties["production_month_local"]["type"] == "keyword"
    assert properties["creator_normalized"]["type"] == "keyword"
    assert properties["has_timezone_unknown"]["type"] == "boolean"
    assert properties["temporal_observations"]["type"] == "nested"
    assert properties["value_observations"] == {"type": "object", "enabled": False}
    assert all(value.get("type") != "text" for value in properties.values())


def test_projection_positive_negative_and_conflict_evidence():
    projection = _projection()
    positive = MetadataFilter(clauses=(_temporal_clause("2024-03"),))
    negative = MetadataFilter(clauses=(_temporal_clause("2024-03", negated=True),))

    positive_result = evaluate_metadata_filter_projection(
        positive, document_id=_ENTITY, projection=projection
    )
    negative_result = evaluate_metadata_filter_projection(
        negative, document_id=_ENTITY, projection=projection
    )

    assert positive_result.result == MetadataTruthValue.TRUE
    assert positive_result.matched_observations[0].source == "pdf_info"
    assert "SOURCE_CONFLICT" in positive_result.conflict_flags
    assert negative_result.result == MetadataTruthValue.FALSE


@pytest.mark.parametrize(
    ("operator", "values", "expected"),
    [
        (MetadataFilterOperator.EQUAL, ("2024-03",), MetadataTruthValue.TRUE),
        (MetadataFilterOperator.IN, ("2024-02", "2024-04"), MetadataTruthValue.TRUE),
        (MetadataFilterOperator.BETWEEN, ("2024-02", "2024-03"), MetadataTruthValue.TRUE),
        (MetadataFilterOperator.BEFORE, ("2024-04",), MetadataTruthValue.TRUE),
        (MetadataFilterOperator.AFTER, ("2024-04",), MetadataTruthValue.FALSE),
    ],
)
def test_projection_supports_every_temporal_comparison_operator(operator, values, expected):
    clause = MetadataFilterClause(
        field=MetadataFilterField.PRODUCTION_MONTH,
        operator=operator,
        values=values,
        calendar_basis=CalendarBasis.SOURCE_LOCAL,
        source_policy=MetadataDateSourcePolicy.ANY_VALID_PRODUCTION_OBSERVATION,
    )
    result = evaluate_metadata_filter_projection(
        MetadataFilter(clauses=(clause,)),
        document_id=_ENTITY,
        projection=_projection(),
    )
    assert result.result == expected
    assert "filter.production_month_local" in str(
        compile_metadata_filter_to_opensearch(
            MetadataFilter(clauses=(clause,)),
            boundary=MetadataProjectionQueryBoundary.DLS_SCOPED_OPENSEARCH_CLIENT,
        )
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (MetadataFilterField.MIME, "application/pdf"),
        (MetadataFilterField.FORMAT_FAMILY, "pdf"),
        (MetadataFilterField.EXTENSION, "pdf"),
        (MetadataFilterField.SOURCE_DOCUMENT_TYPE, "invoice"),
        (MetadataFilterField.SOURCE_SYSTEM, "OPENARCHIVER"),
        (MetadataFilterField.SOURCE_ENTITY_TYPE, "email_attachment"),
        (MetadataFilterField.CONNECTOR, "openarchiver"),
        (MetadataFilterField.CREATOR_OBSERVATION, "ALICE  SMITH"),
        (MetadataFilterField.PRODUCER_OBSERVATION, "LibreOffice 7"),
        (MetadataFilterField.FILENAME_BASENAME, "rapport final"),
        (MetadataFilterField.BINARY_SHA256, "A" * 64),
        (MetadataFilterField.HAS_TEMPORAL_CONFLICT, "true"),
        (MetadataFilterField.HAS_METADATA_CONFLICT, "true"),
    ],
)
def test_type_source_creator_filename_hash_and_conflict_filters_are_exact(field, value):
    metadata_filter = MetadataFilter(
        clauses=(
            MetadataFilterClause(
                field=field,
                operator=MetadataFilterOperator.EQUAL,
                values=(value,),
            ),
        )
    )
    evaluation = evaluate_metadata_filter_projection(
        metadata_filter,
        document_id=_ENTITY,
        projection=_projection(),
    )
    assert evaluation.result == MetadataTruthValue.TRUE
    assert compile_metadata_filter_to_opensearch(
        metadata_filter,
        boundary=MetadataProjectionQueryBoundary.DLS_SCOPED_OPENSEARCH_CLIENT,
    )


def test_source_entity_type_never_populates_explicit_source_family():
    projection = _projection()
    family_filter = MetadataFilter(
        clauses=(
            MetadataFilterClause(
                field=MetadataFilterField.SOURCE_ENTITY_FAMILY,
                operator=MetadataFilterOperator.EQUAL,
                values=("email_attachment",),
            ),
        )
    )
    assert projection.source_entity_types == ("email_attachment",)
    assert projection.source_entity_families == ()
    assert (
        evaluate_metadata_filter_projection(
            family_filter, document_id=_ENTITY, projection=projection
        ).result
        == MetadataTruthValue.FALSE
    )


def test_timezone_unknown_local_value_never_synthesizes_utc_match():
    local = MetadataFilterClause(
        field=MetadataFilterField.MODIFICATION_MONTH,
        operator=MetadataFilterOperator.EQUAL,
        values=("2023-07",),
        calendar_basis=CalendarBasis.SOURCE_LOCAL,
        source_policy=MetadataDateSourcePolicy.ANY_VALID_MODIFICATION_OBSERVATION,
    )
    utc = local.model_copy(update={"calendar_basis": CalendarBasis.UTC})
    projection = _projection()

    assert evaluate_metadata_filter_projection(
        MetadataFilter(clauses=(local,)), document_id=_ENTITY, projection=projection
    ).result == MetadataTruthValue.TRUE
    assert evaluate_metadata_filter_projection(
        MetadataFilter(clauses=(utc,)), document_id=_ENTITY, projection=projection
    ).result == MetadataTruthValue.UNKNOWN
    assert projection.modification_month_utc == ()


def test_projection_missing_and_missing_explicit_source_are_unknown_under_negation():
    clause = _temporal_clause("2024-03", negated=True, explicit_source="ooxml_core")
    metadata_filter = MetadataFilter(clauses=(clause,))

    assert (
        evaluate_metadata_filter_projection(
            metadata_filter, document_id=_ENTITY, projection=_projection()
        ).result
        == MetadataTruthValue.UNKNOWN
    )
    assert (
        evaluate_metadata_filter_projection(
            metadata_filter, document_id="missing", projection=None
        ).result
        == MetadataTruthValue.UNKNOWN
    )


def test_not_exists_distinguishes_known_absence_from_missing_projection():
    profile = DocumentMetadataProfile(entity_id=_ENTITY)
    projection = generate_metadata_filter_projection(
        profile,
        source_context=MetadataFilterProjectionSourceContext(source_entity_id=_ENTITY),
    )
    clause = MetadataFilterClause(
        field=MetadataFilterField.PRODUCTION_MONTH,
        operator=MetadataFilterOperator.NOT_EXISTS,
        calendar_basis=CalendarBasis.SOURCE_LOCAL,
        source_policy=MetadataDateSourcePolicy.ANY_VALID_PRODUCTION_OBSERVATION,
    )
    metadata_filter = MetadataFilter(clauses=(clause,))

    assert (
        evaluate_metadata_filter_projection(
            metadata_filter, document_id=_ENTITY, projection=projection
        ).result
        == MetadataTruthValue.TRUE
    )
    assert (
        evaluate_metadata_filter_projection(
            metadata_filter, document_id="missing", projection=None
        ).result
        == MetadataTruthValue.UNKNOWN
    )


@pytest.mark.parametrize(
    ("left", "right", "and_expected", "or_expected"),
    [
        (MetadataTruthValue.TRUE, MetadataTruthValue.TRUE, MetadataTruthValue.TRUE, MetadataTruthValue.TRUE),
        (MetadataTruthValue.TRUE, MetadataTruthValue.FALSE, MetadataTruthValue.FALSE, MetadataTruthValue.TRUE),
        (MetadataTruthValue.TRUE, MetadataTruthValue.UNKNOWN, MetadataTruthValue.UNKNOWN, MetadataTruthValue.TRUE),
        (MetadataTruthValue.FALSE, MetadataTruthValue.TRUE, MetadataTruthValue.FALSE, MetadataTruthValue.TRUE),
        (MetadataTruthValue.FALSE, MetadataTruthValue.FALSE, MetadataTruthValue.FALSE, MetadataTruthValue.FALSE),
        (MetadataTruthValue.FALSE, MetadataTruthValue.UNKNOWN, MetadataTruthValue.FALSE, MetadataTruthValue.UNKNOWN),
        (MetadataTruthValue.UNKNOWN, MetadataTruthValue.TRUE, MetadataTruthValue.UNKNOWN, MetadataTruthValue.TRUE),
        (MetadataTruthValue.UNKNOWN, MetadataTruthValue.FALSE, MetadataTruthValue.FALSE, MetadataTruthValue.UNKNOWN),
        (MetadataTruthValue.UNKNOWN, MetadataTruthValue.UNKNOWN, MetadataTruthValue.UNKNOWN, MetadataTruthValue.UNKNOWN),
    ],
)
def test_strong_kleene_and_or_truth_tables(left, right, and_expected, or_expected):
    assert truth_and((left, right)) == and_expected
    assert truth_or((left, right)) == or_expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (MetadataTruthValue.TRUE, MetadataTruthValue.FALSE),
        (MetadataTruthValue.FALSE, MetadataTruthValue.TRUE),
        (MetadataTruthValue.UNKNOWN, MetadataTruthValue.UNKNOWN),
    ],
)
def test_strong_kleene_not_truth_table(value, expected):
    assert truth_not(value) == expected


def test_recursive_and_or_not_uses_three_valued_logic():
    true_leaf = MetadataFilterExpression(clause=_temporal_clause("2024-03"))
    unknown_leaf = MetadataFilterExpression(
        clause=_temporal_clause("2024-03", explicit_source="ooxml_core")
    )
    expression = MetadataFilterExpression(
        operator=MetadataFilterBooleanOperator.NOT,
        children=(
            MetadataFilterExpression(
                operator=MetadataFilterBooleanOperator.AND,
                children=(true_leaf, unknown_leaf),
            ),
        ),
    )
    result = evaluate_metadata_filter_projection(
        MetadataFilter(expression=expression),
        document_id=_ENTITY,
        projection=_projection(),
    )
    assert result.result == MetadataTruthValue.UNKNOWN


def test_recursive_expression_canonical_serialization_is_commutative_for_and_or():
    march = MetadataFilterExpression(clause=_temporal_clause("2024-03"))
    april = MetadataFilterExpression(clause=_temporal_clause("2024-04"))
    forward = MetadataFilter(
        expression=MetadataFilterExpression(
            operator=MetadataFilterBooleanOperator.OR,
            children=(march, april),
        )
    )
    reverse = MetadataFilter(
        expression=MetadataFilterExpression(
            operator=MetadataFilterBooleanOperator.OR,
            children=(april, march),
        )
    )
    assert forward.canonical_json() == reverse.canonical_json()
    assert forward.calculate_sha256() == reverse.calculate_sha256()


def test_opensearch_compiler_emits_only_true_set_and_requires_dls_boundary():
    positive = MetadataFilter(clauses=(_temporal_clause("2024-03"),))
    negative = MetadataFilter(clauses=(_temporal_clause("2024-03", negated=True),))
    positive_query = compile_metadata_filter_to_opensearch(
        positive,
        boundary=MetadataProjectionQueryBoundary.DLS_SCOPED_OPENSEARCH_CLIENT,
    )
    negative_query = compile_metadata_filter_to_opensearch(
        negative,
        boundary=MetadataProjectionQueryBoundary.DLS_SCOPED_OPENSEARCH_CLIENT,
    )
    encoded_positive = str(positive_query)
    encoded_negative = str(negative_query)

    assert "filter.projection_sha256" in encoded_positive
    assert "filter.production_month_local" in encoded_positive
    assert "filter.production_month_local" in encoded_negative
    assert "must_not" in encoded_negative
    with pytest.raises(ValueError, match="DLS-scoped"):
        compile_metadata_filter_to_opensearch(positive, boundary="ADMIN_CLIENT")


def test_unknown_explicit_observation_source_fails_closed():
    metadata_filter = MetadataFilter(
        clauses=(_temporal_clause("2024-03", explicit_source="unregistered-source"),)
    )
    with pytest.raises(ValueError, match="unsupported explicit"):
        compile_metadata_filter_to_opensearch(
            metadata_filter,
            boundary=MetadataProjectionQueryBoundary.DLS_SCOPED_OPENSEARCH_CLIENT,
        )
    with pytest.raises(ValueError, match="unsupported explicit"):
        evaluate_metadata_filter_projection(
            metadata_filter,
            document_id=_ENTITY,
            projection=_projection(),
        )


def test_compiled_and_or_not_truth_set_is_structurally_explicit():
    leaves = tuple(
        MetadataFilterExpression(clause=_temporal_clause(value))
        for value in ("2024-03", "2024-04")
    )
    expression = MetadataFilterExpression(
        operator=MetadataFilterBooleanOperator.NOT,
        children=(
            MetadataFilterExpression(
                operator=MetadataFilterBooleanOperator.OR,
                children=leaves,
            ),
        ),
    )
    query = compile_metadata_filter_to_opensearch(
        MetadataFilter(expression=expression),
        boundary=MetadataProjectionQueryBoundary.DLS_SCOPED_OPENSEARCH_CLIENT,
    )
    assert "must_not" in str(query)
    assert "minimum_should_match" not in str(query)


def test_side_document_copies_only_required_acl_and_is_stable():
    document = build_projection_side_document(
        _projection(),
        source_document_id="a" * 64,
        source_entity_id=_ENTITY,
        representative_chunk_id="chunk-1",
        owner="user-1",
        allowed_users=("b", "a", "a"),
        allowed_groups=("group",),
        allowed_principals=("u:ms:1",),
    )
    assert document.allowed_users == ("a", "b")
    assert document.owner == "user-1"
    assert "owner_name" not in document.model_dump()
    assert build_projection_side_document(
        _projection(),
        source_document_id="a" * 64,
        source_entity_id=_ENTITY,
        representative_chunk_id="chunk-1",
        owner="user-1",
    ).projection_document_id == document.projection_document_id


class _FakeIndices:
    def __init__(self, parent) -> None:
        self.parent = parent

    async def exists(self, *, index):
        return index in self.parent.indices_data

    async def create(self, *, index, body):
        self.parent.indices_data[index] = {}
        self.parent.mappings[index] = body

    async def delete(self, *, index):
        del self.parent.indices_data[index]


class _FakeClient:
    def __init__(self) -> None:
        self.indices_data: dict[str, dict[str, dict]] = {}
        self.mappings: dict[str, dict] = {}
        self.indices = _FakeIndices(self)
        self.bulk_writes = 0

    async def mget(self, *, index, body):
        values = self.indices_data[index]
        return {
            "docs": [
                (
                    {"_id": item_id, "found": True, "_source": values[item_id]}
                    if item_id in values
                    else {"_id": item_id, "found": False}
                )
                for item_id in body["ids"]
            ]
        }

    async def bulk(self, *, body, refresh):
        assert refresh is True
        for offset in range(0, len(body), 2):
            action, source = body[offset : offset + 2]
            index = action["index"]["_index"]
            item_id = action["index"]["_id"]
            self.indices_data[index][item_id] = source
            self.bulk_writes += 1
        return {"errors": False}


@pytest.mark.asyncio
async def test_canary_is_idempotent_verifiable_and_rolls_back_only_side_index():
    client = _FakeClient()
    canary = MetadataFilterProjectionCanary(
        client,
        index_name="documents-metadata-filter-projection-canary-unit",
    )
    await canary.create()
    document = build_projection_side_document(
        _projection(),
        source_document_id="a" * 64,
        source_entity_id=_ENTITY,
        representative_chunk_id="chunk-1",
        owner=None,
    )

    first = await canary.apply([document], enforce_cohort_bounds=False)
    second = await canary.apply([document], enforce_cohort_bounds=False)
    verified = await canary.verify([document])

    assert first == {"attempted": 1, "changed": 1, "unchanged": 0}
    assert second == {"attempted": 1, "changed": 0, "unchanged": 1}
    assert verified == {"expected": 1, "verified": 1}
    assert client.bulk_writes == 1
    assert await canary.rollback() is True
    assert client.indices_data == {}


def test_canary_rejects_nonisolated_index_and_unbounded_cohort():
    with pytest.raises(ValueError, match="isolated"):
        MetadataFilterProjectionCanary(_FakeClient(), index_name="documents")
    documents = [
        build_projection_side_document(
            _projection(),
            source_document_id=f"{number:064x}",
            source_entity_id=f"urn:test:{number}",
            representative_chunk_id=f"chunk-{number}",
            owner=None,
        )
        for number in range(99)
    ]
    with pytest.raises(ValueError, match="100"):
        MetadataFilterProjectionCanary._validate_documents(
            documents, enforce_cohort_bounds=True
        )


@pytest.mark.parametrize("config_path", ["securityconfig/roles.yml", "cloud_securityconfig/roles.yml"])
def test_projection_side_index_inherits_the_existing_document_dls_role(config_path):
    repository = Path(__file__).resolve().parents[3]
    roles = yaml.safe_load((repository / config_path).read_text(encoding="utf-8"))
    permissions = roles["openrag_user_role"]["index_permissions"]
    matching = [
        item
        for item in permissions
        if "documents*" in item.get("index_patterns", []) and item.get("dls")
    ]
    assert len(matching) == 1
    dls = matching[0]["dls"]
    assert all(
        field in dls
        for field in ("owner", "allowed_users", "allowed_principals", "minimum_should_match")
    )


def test_truth_table_fixture_is_exhaustive():
    assert len(list(product(MetadataTruthValue, repeat=2))) == 9
