"""Contract tests for openrag.document-metadata v1 extraction and normalization."""

from __future__ import annotations

import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject

from models.document_metadata import (
    DocumentMetadataProfile,
    MetadataExposureClass,
    MetadataNormalizationStatus,
    MetadataObservation,
    MetadataSectionName,
    MetadataSourceType,
    MetadataTrustClass,
    document_metadata_mapping,
)
from services.document_metadata_extractor import (
    ArchiveMetadataContext,
    MetadataExtractionError,
    extract_document_metadata,
    parse_metadata_datetime,
    supported_metadata_formats,
)


def _context(filename: str, **kwargs: object) -> ArchiveMetadataContext:
    return ArchiveMetadataContext(
        entity_id="urn:openrag:test:document-1",
        archive_source="test_archive",
        archive_object_id="object-1",
        original_name=filename,
        archive_storage_locator=f"objects/{filename}",
        ingested_at="2026-09-02T08:00:00+02:00",
        **kwargs,
    )


def _fact(profile: DocumentMetadataProfile, field: str, source: str | None = None):
    matches = [
        item
        for item in profile.observations()
        if item.field == field and (source is None or item.source == source)
    ]
    assert matches, (field, source)
    return matches[0]


@pytest.mark.parametrize(
    ("raw", "parser", "normalized", "timezone", "status"),
    [
        (
            "2021-06-12T09:14:22+02:00",
            "iso",
            "2021-06-12T09:14:22+02:00",
            "+02:00",
            "timezone_explicit",
        ),
        (
            "2021-06-12T09:14:22",
            "iso",
            "2021-06-12T09:14:22",
            "UNKNOWN",
            "timezone_unknown",
        ),
        (
            "D:20210612091422+02'00'",
            "pdf",
            "2021-06-12T09:14:22+02:00",
            "+02:00",
            "timezone_explicit",
        ),
        ("2021-99-99", "iso", None, "UNKNOWN", "invalid"),
    ],
)
def test_datetime_normalization_preserves_timezone_contract(
    raw, parser, normalized, timezone, status
):
    assert parse_metadata_datetime(raw, parser=parser) == (normalized, timezone, status)


def test_observation_requires_source_and_raw_timestamp():
    base = {
        "section": "embedded",
        "field": "embedded_created_at",
        "value": "2021-06-12T09:14:22",
        "raw_value": "2021-06-12T09:14:22",
        "source": "ooxml_core_properties",
        "source_type": "format_native",
        "trust_class": "embedded_document_metadata",
        "extracted_at": datetime.now(UTC),
        "normalization_status": "timezone_unknown",
        "timezone": "UNKNOWN",
    }
    assert MetadataObservation.model_validate(base).source == "ooxml_core_properties"

    without_source = dict(base)
    without_source.pop("source")
    with pytest.raises(ValidationError):
        MetadataObservation.model_validate(without_source)

    without_raw = dict(base)
    without_raw.pop("raw_value")
    with pytest.raises(ValidationError, match="preserve raw_value"):
        MetadataObservation.model_validate(without_raw)


def test_digest_is_idempotent_across_extraction_wall_times(tmp_path: Path):
    path = tmp_path / "notes.txt"
    path.write_text("same bytes")
    context = _context(
        path.name,
        filesystem_mtime="2026-01-01T12:00:00",
        archived_at="2026-02-01T12:00:00Z",
    )

    first = extract_document_metadata(
        path, context, extracted_at=datetime(2026, 1, 1, tzinfo=UTC)
    ).profile
    second = extract_document_metadata(
        path, context, extracted_at=datetime(2026, 9, 1, tzinfo=UTC)
    ).profile

    assert first.metadata_facts_sha256 == second.metadata_facts_sha256
    assert _fact(first, "filesystem_mtime").timezone == "UNKNOWN"
    assert _fact(first, "archived_at").timezone == "Z"


def test_temporary_file_stat_timestamps_are_not_observed(tmp_path: Path):
    path = tmp_path / "downloaded-copy.txt"
    path.write_text("archive payload")

    profile = extract_document_metadata(path, _context(path.name)).profile

    fields = {item.field for item in profile.filesystem}
    assert "filesystem_mtime" not in fields
    assert "filesystem_ctime" not in fields
    assert "filesystem_birthtime" not in fields
    assert "source_path" not in fields
    assert _fact(profile, "archive_storage_locator").value == "objects/downloaded-copy.txt"


def test_ooxml_core_and_app_properties_stay_source_qualified(tmp_path: Path):
    path = tmp_path / "sample.docx"
    core = b"""<?xml version='1.0' encoding='UTF-8'?>
      <cp:coreProperties xmlns:cp='urn:cp' xmlns:dc='urn:dc' xmlns:dcterms='urn:dcterms'>
        <dc:title>Board report</dc:title><dc:creator>CEO</dc:creator>
        <cp:lastModifiedBy>Reviewer</cp:lastModifiedBy>
        <dcterms:created>1990-01-01T01:02:03</dcterms:created>
        <dcterms:modified>2024-05-06T07:08:09Z</dcterms:modified>
        <cp:revision>7</cp:revision>
      </cp:coreProperties>"""
    app = b"""<?xml version='1.0'?><Properties xmlns='urn:app'>
      <Application>LibreOffice</Application><AppVersion>24.2</AppVersion>
      <Company>Example Corp</Company><Manager>A Manager</Manager></Properties>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("docProps/core.xml", core)
        archive.writestr("docProps/app.xml", app)

    profile = extract_document_metadata(
        path,
        _context(path.name, filesystem_mtime="2026-08-01T12:00:00Z"),
    ).profile

    creator = _fact(profile, "creator")
    assert creator.value == "CEO"
    assert creator.source == "ooxml_core_properties"
    assert creator.trust_class is MetadataTrustClass.EMBEDDED_DOCUMENT_METADATA
    assert creator.exposure_class is MetadataExposureClass.INTERNAL
    assert _fact(profile, "embedded_created_at").timezone == "UNKNOWN"
    assert _fact(profile, "embedded_modified_at").timezone == "Z"
    assert _fact(profile, "embedded_modified_at").value == "2024-05-06T07:08:09+00:00"
    assert _fact(profile, "filesystem_mtime").value == "2026-08-01T12:00:00+00:00"
    assert (
        _fact(profile, "embedded_modified_at").source != _fact(profile, "filesystem_mtime").source
    )
    assert _fact(profile, "last_modified_by").value == "Reviewer"
    assert _fact(profile, "creator_application").value == "LibreOffice"


def test_odf_meta_xml_is_supported(tmp_path: Path):
    path = tmp_path / "sample.odt"
    metadata = b"""<?xml version='1.0'?><office:document-meta
      xmlns:office='urn:office' xmlns:dc='urn:dc' xmlns:meta='urn:meta'>
      <office:meta><dc:title>ODF title</dc:title><meta:initial-creator>Alice</meta:initial-creator>
      <meta:creation-date>2020-02-03T04:05:06+01:00</meta:creation-date>
      <meta:generator>Writer</meta:generator></office:meta></office:document-meta>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("meta.xml", metadata)

    profile = extract_document_metadata(path, _context(path.name)).profile

    assert _fact(profile, "title").value == "ODF title"
    assert _fact(profile, "creator").value == "Alice"
    assert _fact(profile, "embedded_created_at").timezone == "+01:00"


def _write_pdf_with_info_and_xmp(path: Path, *, malformed_xmp: bool = False) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_metadata(
        {
            "/Title": "Info title",
            "/Author": "CEO",
            "/CreationDate": "D:19900101000000",
        }
    )
    xmp = (
        b"<not-closed>"
        if malformed_xmp
        else b"""<?xpacket begin=''?><x:xmpmeta xmlns:x='adobe:ns:meta/'
          xmlns:dc='http://purl.org/dc/elements/1.1/'
          xmlns:xmp='http://ns.adobe.com/xap/1.0/'>
          <dc:title>XMP title</dc:title><xmp:CreateDate>2022-03-04T05:06:07Z</xmp:CreateDate>
          </x:xmpmeta>"""
    )
    stream = DecodedStreamObject()
    stream.set_data(xmp)
    stream.update(
        {NameObject("/Type"): NameObject("/Metadata"), NameObject("/Subtype"): NameObject("/XML")}
    )
    writer._root_object[NameObject("/Metadata")] = writer._add_object(stream)
    with path.open("wb") as output:
        writer.write(output)


def test_pdf_info_and_xmp_conflicts_are_preserved(tmp_path: Path):
    path = tmp_path / "conflict.pdf"
    _write_pdf_with_info_and_xmp(path)

    profile = extract_document_metadata(path, _context(path.name)).profile

    assert _fact(profile, "title", "pdf_info_dictionary").value == "Info title"
    assert _fact(profile, "title", "pdf_xmp").value == "XMP title"
    assert _fact(profile, "embedded_created_at", "pdf_info_dictionary").timezone == "UNKNOWN"
    assert _fact(profile, "embedded_created_at", "pdf_xmp").timezone == "Z"
    conflicts = {item.field: item for item in profile.conflicts}
    assert set(conflicts["title"].sources) == {"pdf_info_dictionary", "pdf_xmp"}
    assert conflicts["embedded_created_at"].resolution == "unresolved_observations_preserved"
    assert _fact(profile, "author").trust_class is MetadataTrustClass.EMBEDDED_DOCUMENT_METADATA


def test_malformed_pdf_xmp_fails_closed(tmp_path: Path):
    path = tmp_path / "malformed.pdf"
    _write_pdf_with_info_and_xmp(path, malformed_xmp=True)

    with pytest.raises(MetadataExtractionError, match="malformed PDF XMP"):
        extract_document_metadata(path, _context(path.name))


def test_image_sensitive_device_metadata_is_redacted(tmp_path: Path):
    path = tmp_path / "photo.jpg"
    image = Image.new("RGB", (2, 2), "white")
    exif = Image.Exif()
    exif[315] = "Photographer"
    exif[36867] = "2024:01:02 03:04:05"
    exif[42033] = "SECRET-SERIAL-123"
    image.save(path, exif=exif)

    profile = extract_document_metadata(path, _context(path.name)).profile

    assert _fact(profile, "creator").value == "Photographer"
    marker = _fact(profile, "sensitive_metadata_present")
    assert marker.normalization_status is MetadataNormalizationStatus.REDACTED_SENSITIVE
    assert "SECRET-SERIAL-123" not in profile.model_dump_json()


def test_email_header_metadata_remains_intrinsic_not_parent_provenance(tmp_path: Path):
    path = tmp_path / "mail.eml"
    path.write_bytes(
        b"Subject: Archive notice\r\nFrom: sender@example.test\r\n"
        b"Date: Tue, 2 Sep 2025 08:00:00 +0200\r\n"
        b"Message-ID: <m1@example.test>\r\n\r\nBody"
    )

    profile = extract_document_metadata(path, _context(path.name)).profile

    assert _fact(profile, "subject").source == "rfc5322_headers"
    assert _fact(profile, "embedded_sent_at").timezone == "+02:00"
    assert not profile.archive or all(item.field != "attachment_of" for item in profile.archive)


def test_profile_mapping_is_observational_and_not_searchable():
    mapping = document_metadata_mapping()

    assert mapping["document_metadata_profile"] == {"type": "object", "enabled": False}
    assert "copy_to" not in repr(mapping)
    assert {".pdf", ".docx", ".xlsx", ".pptx", ".odt", ".ods"} <= set(supported_metadata_formats())


def test_profile_rejects_observation_in_wrong_section():
    observation = MetadataObservation(
        section=MetadataSectionName.EMBEDDED,
        field="author",
        value="CEO",
        source="pdf_info_dictionary",
        source_type=MetadataSourceType.FORMAT_NATIVE,
        trust_class=MetadataTrustClass.EMBEDDED_DOCUMENT_METADATA,
        extracted_at=datetime.now(UTC),
    )

    with pytest.raises(ValidationError, match="must remain in identity"):
        DocumentMetadataProfile(entity_id="urn:test", identity=[observation])
