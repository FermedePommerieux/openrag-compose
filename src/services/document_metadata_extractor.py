"""Bounded native metadata extraction from read-only archived originals."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import zipfile
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from email import policy
from email.parser import BytesHeaderParser
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from defusedxml import ElementTree

from models.document_metadata import (
    DOCUMENT_METADATA_EXTRACTOR_NAME,
    DOCUMENT_METADATA_EXTRACTOR_VERSION,
    DOCUMENT_METADATA_NORMALIZATION_ID,
    DOCUMENT_METADATA_NORMALIZATION_VERSION,
    DOCUMENT_METADATA_PROFILE_ID,
    DOCUMENT_METADATA_PROFILE_VERSION,
    METADATA_RESOLUTION_POLICY_ID,
    METADATA_RESOLUTION_POLICY_VERSION,
    DocumentMetadataProfile,
    MetadataConflict,
    MetadataExposureClass,
    MetadataNormalizationStatus,
    MetadataObservation,
    MetadataSectionName,
    MetadataSourceType,
    MetadataTrustClass,
)

MAX_METADATA_MEMBER_BYTES = 4 * 1024 * 1024
MAX_EMAIL_HEADERS_BYTES = 1024 * 1024
MAX_PDF_XMP_BYTES = 8 * 1024 * 1024
MAX_SOURCE_FILE_BYTES = 1024 * 1024 * 1024
UNKNOWN_TIMEZONE = "UNKNOWN"


class UnsupportedMetadataFormatError(ValueError):
    """Raised only when no safe v1 native extractor exists for the format."""


class MetadataExtractionError(RuntimeError):
    """Raised when a supported original cannot be safely parsed."""


@dataclass(frozen=True)
class ArchiveMetadataContext:
    """Verified mapping context, kept distinct from intrinsic file metadata."""

    entity_id: str
    archive_source: str
    archive_object_id: str
    original_name: str
    archive_storage_locator: str
    mime_type: str | None = None
    archived_at: str | datetime | None = None
    archive_created_at: str | datetime | None = None
    archive_modified_at: str | datetime | None = None
    filesystem_birthtime: str | datetime | None = None
    filesystem_mtime: str | datetime | None = None
    filesystem_ctime: str | datetime | None = None
    filesystem_source_path: str | None = None
    ingested_at: str | datetime | None = None
    parent_entity_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExtractionResult:
    profile: DocumentMetadataProfile
    format_name: str
    bytes_read: int
    elapsed_ms: float
    native_metadata_supported: bool
    warnings: tuple[str, ...] = field(default_factory=tuple)


def _clean_scalar(value: object) -> str | int | float | bool | list[str] | None:
    if value is None:
        return None
    if isinstance(value, bool | int | float):
        return value
    if isinstance(value, (list, tuple, set)):
        result = [str(item).strip() for item in value if str(item).strip()]
        return list(dict.fromkeys(result)) or None
    normalized = re.sub(r"\s+", " ", str(value)).strip(" \x00")
    return normalized or None


def _timezone_label(value: datetime) -> str:
    offset = value.utcoffset()
    if offset is None:
        return UNKNOWN_TIMEZONE
    if offset == timedelta(0):
        return "Z"
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    return f"{sign}{total_minutes // 60:02d}:{total_minutes % 60:02d}"


_PDF_DATE = re.compile(
    r"^D:(?P<year>\d{4})(?P<month>\d{2})?(?P<day>\d{2})?"
    r"(?P<hour>\d{2})?(?P<minute>\d{2})?(?P<second>\d{2})?"
    r"(?P<tz>Z|[+-]\d{2}(?:'?\d{2}'?)?)?$"
)


def parse_metadata_datetime(value: object, *, parser: str = "iso") -> tuple[str | None, str, str]:
    """Return normalized value, timezone label, and normalization status."""
    raw = str(value).strip() if value is not None else ""
    if not raw:
        return None, UNKNOWN_TIMEZONE, MetadataNormalizationStatus.INVALID.value
    try:
        if parser == "pdf":
            match = _PDF_DATE.fullmatch(raw)
            if not match:
                raise ValueError("invalid PDF date")
            fields = match.groupdict()
            tz_raw = fields["tz"]
            tzinfo = None
            if tz_raw == "Z":
                tzinfo = UTC
            elif tz_raw:
                compact = tz_raw.replace("'", "")
                sign = 1 if compact[0] == "+" else -1
                tzinfo = timezone(
                    sign * timedelta(hours=int(compact[1:3]), minutes=int(compact[3:5] or 0))
                )
            parsed = datetime(
                int(fields["year"]),
                int(fields["month"] or 1),
                int(fields["day"] or 1),
                int(fields["hour"] or 0),
                int(fields["minute"] or 0),
                int(fields["second"] or 0),
                tzinfo=tzinfo,
            )
        elif parser == "rfc5322":
            parsed = parsedate_to_datetime(raw)
        else:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None, UNKNOWN_TIMEZONE, MetadataNormalizationStatus.INVALID.value
    timezone_name = _timezone_label(parsed)
    status = (
        MetadataNormalizationStatus.TIMEZONE_UNKNOWN.value
        if parsed.tzinfo is None
        else MetadataNormalizationStatus.TIMEZONE_EXPLICIT.value
    )
    return parsed.isoformat(), timezone_name, status


class _ProfileBuilder:
    def __init__(self, context: ArchiveMetadataContext, extracted_at: datetime):
        self.context = context
        self.extracted_at = extracted_at
        self.sections: dict[MetadataSectionName, list[MetadataObservation]] = defaultdict(list)
        self.warnings: list[str] = []

    def add(
        self,
        section: MetadataSectionName,
        field_name: str,
        value: object,
        *,
        source: str,
        source_type: MetadataSourceType,
        trust: MetadataTrustClass,
        raw_value: object | None = None,
        status: MetadataNormalizationStatus = MetadataNormalizationStatus.NORMALIZED,
        timezone_name: str | None = None,
    ) -> None:
        cleaned = _clean_scalar(value)
        cleaned_raw = _clean_scalar(raw_value)
        if cleaned is None and cleaned_raw is None:
            return
        self.sections[section].append(
            MetadataObservation(
                section=section,
                field=field_name,
                value=cleaned,
                raw_value=cleaned_raw,
                source=source,
                source_type=source_type,
                trust_class=trust,
                exposure_class=MetadataExposureClass.INTERNAL,
                extracted_at=self.extracted_at,
                normalization_status=status,
                timezone=timezone_name,
            )
        )

    def add_datetime(
        self,
        section: MetadataSectionName,
        field_name: str,
        raw_value: object,
        *,
        source: str,
        source_type: MetadataSourceType,
        trust: MetadataTrustClass,
        parser: str = "iso",
    ) -> None:
        value, timezone_name, status = parse_metadata_datetime(raw_value, parser=parser)
        self.add(
            section,
            field_name,
            value,
            raw_value=raw_value,
            source=source,
            source_type=source_type,
            trust=trust,
            status=MetadataNormalizationStatus(status),
            timezone_name=timezone_name,
        )

    def build(self) -> DocumentMetadataProfile:
        conflicts: list[MetadataConflict] = []
        by_field: dict[str, list[MetadataObservation]] = defaultdict(list)
        for item in self.sections[MetadataSectionName.EMBEDDED]:
            by_field[item.field].append(item)
        for field_name, observations in sorted(by_field.items()):
            values = {
                str(item.value if item.value is not None else item.raw_value)
                for item in observations
                if item.value is not None or item.raw_value is not None
            }
            if len(values) > 1:
                conflicts.append(
                    MetadataConflict(
                        field=field_name,
                        values=sorted(values),
                        sources=sorted({item.source for item in observations}),
                    )
                )
        for section_values in self.sections.values():
            section_values.sort(key=lambda item: (item.field, item.source, str(item.value)))
        return DocumentMetadataProfile(
            entity_id=self.context.entity_id,
            identity=self.sections[MetadataSectionName.IDENTITY],
            embedded=self.sections[MetadataSectionName.EMBEDDED],
            filesystem=self.sections[MetadataSectionName.FILESYSTEM],
            archive=self.sections[MetadataSectionName.ARCHIVE],
            ingestion=self.sections[MetadataSectionName.INGESTION],
            conflicts=conflicts,
        )


def _bounded_member(archive: zipfile.ZipFile, name: str) -> bytes | None:
    if len(archive.infolist()) > 10_000:
        raise MetadataExtractionError("archive package has too many members")
    try:
        info = archive.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_METADATA_MEMBER_BYTES:
        raise MetadataExtractionError(f"metadata member {name} exceeds the v1 limit")
    if info.compress_size and info.file_size / info.compress_size > 1000:
        raise MetadataExtractionError(f"metadata member {name} has an unsafe compression ratio")
    value = archive.read(info)
    if len(value) != info.file_size:
        raise MetadataExtractionError(f"metadata member {name} was truncated")
    return value


def _xml_texts(root: Any, local_name: str) -> list[str]:
    return [
        text
        for item in root.iter()
        if str(item.tag).rsplit("}", 1)[-1] == local_name
        and (text := " ".join("".join(item.itertext()).split()))
    ]


def _extract_ooxml(path: Path, builder: _ProfileBuilder) -> None:
    core_fields = {
        "title": "title",
        "subject": "subject",
        "creator": "creator",
        "keywords": "keywords",
        "category": "category",
        "lastModifiedBy": "last_modified_by",
        "revision": "revision",
    }
    time_fields = {"created": "embedded_created_at", "modified": "embedded_modified_at"}
    app_fields = {
        "Application": "creator_application",
        "AppVersion": "application_version",
        "Company": "company",
        "Manager": "manager",
    }
    try:
        with zipfile.ZipFile(path) as archive:
            core = _bounded_member(archive, "docProps/core.xml")
            app = _bounded_member(archive, "docProps/app.xml")
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise MetadataExtractionError(f"invalid Office Open XML package: {exc}") from exc
    if core:
        root = ElementTree.fromstring(core)
        for native_name, field_name in core_fields.items():
            for value in _xml_texts(root, native_name):
                builder.add(
                    MetadataSectionName.EMBEDDED,
                    field_name,
                    value,
                    source="ooxml_core_properties",
                    source_type=MetadataSourceType.FORMAT_NATIVE,
                    trust=MetadataTrustClass.EMBEDDED_DOCUMENT_METADATA,
                )
        for native_name, field_name in time_fields.items():
            for value in _xml_texts(root, native_name):
                builder.add_datetime(
                    MetadataSectionName.EMBEDDED,
                    field_name,
                    value,
                    source="ooxml_core_properties",
                    source_type=MetadataSourceType.FORMAT_NATIVE,
                    trust=MetadataTrustClass.EMBEDDED_DOCUMENT_METADATA,
                )
    if app:
        root = ElementTree.fromstring(app)
        for native_name, field_name in app_fields.items():
            for value in _xml_texts(root, native_name):
                builder.add(
                    MetadataSectionName.EMBEDDED,
                    field_name,
                    value,
                    source="ooxml_extended_properties",
                    source_type=MetadataSourceType.FORMAT_NATIVE,
                    trust=MetadataTrustClass.EMBEDDED_DOCUMENT_METADATA,
                )


def _extract_odf(path: Path, builder: _ProfileBuilder) -> None:
    fields = {
        "title": "title",
        "subject": "subject",
        "keyword": "keywords",
        "creator": "creator",
        "initial-creator": "creator",
        "editing-cycles": "revision",
        "generator": "creator_application",
        "description": "description",
    }
    time_fields = {"creation-date": "embedded_created_at", "date": "embedded_modified_at"}
    try:
        with zipfile.ZipFile(path) as archive:
            metadata = _bounded_member(archive, "meta.xml")
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise MetadataExtractionError(f"invalid ODF package: {exc}") from exc
    if not metadata:
        return
    root = ElementTree.fromstring(metadata)
    for native_name, field_name in fields.items():
        values = _xml_texts(root, native_name)
        if field_name == "keywords" and values:
            builder.add(
                MetadataSectionName.EMBEDDED,
                field_name,
                values,
                source="odf_meta_xml",
                source_type=MetadataSourceType.FORMAT_NATIVE,
                trust=MetadataTrustClass.EMBEDDED_DOCUMENT_METADATA,
            )
            continue
        for value in values:
            builder.add(
                MetadataSectionName.EMBEDDED,
                field_name,
                value,
                source="odf_meta_xml",
                source_type=MetadataSourceType.FORMAT_NATIVE,
                trust=MetadataTrustClass.EMBEDDED_DOCUMENT_METADATA,
            )
    for native_name, field_name in time_fields.items():
        for value in _xml_texts(root, native_name):
            builder.add_datetime(
                MetadataSectionName.EMBEDDED,
                field_name,
                value,
                source="odf_meta_xml",
                source_type=MetadataSourceType.FORMAT_NATIVE,
                trust=MetadataTrustClass.EMBEDDED_DOCUMENT_METADATA,
            )


def _extract_pdf(path: Path, builder: _ProfileBuilder) -> None:
    try:
        from pypdf import PdfReader

        reader = PdfReader(path, strict=False)
        info: Any = reader.metadata or {}
    except Exception as exc:
        raise MetadataExtractionError(f"invalid PDF metadata: {exc}") from exc
    fields = {
        "/Title": "title",
        "/Subject": "subject",
        "/Keywords": "keywords",
        "/Author": "author",
        "/Creator": "creator_application",
        "/Producer": "producer",
    }
    for native_name, field_name in fields.items():
        builder.add(
            MetadataSectionName.EMBEDDED,
            field_name,
            info.get(native_name),
            source="pdf_info_dictionary",
            source_type=MetadataSourceType.FORMAT_NATIVE,
            trust=MetadataTrustClass.EMBEDDED_DOCUMENT_METADATA,
        )
    for native_name, field_name in {
        "/CreationDate": "embedded_created_at",
        "/ModDate": "embedded_modified_at",
    }.items():
        if info.get(native_name) is not None:
            builder.add_datetime(
                MetadataSectionName.EMBEDDED,
                field_name,
                info[native_name],
                source="pdf_info_dictionary",
                source_type=MetadataSourceType.FORMAT_NATIVE,
                trust=MetadataTrustClass.EMBEDDED_DOCUMENT_METADATA,
                parser="pdf",
            )
    try:
        root_object: Any = reader.trailer["/Root"]
        metadata_ref = root_object.get("/Metadata")
        xmp = metadata_ref.get_object().get_data() if metadata_ref is not None else None
    except Exception as exc:
        builder.warnings.append(f"pdf_xmp_unreadable:{type(exc).__name__}")
        xmp = None
    if not xmp:
        return
    if len(xmp) > MAX_PDF_XMP_BYTES:
        raise MetadataExtractionError("PDF XMP exceeds the v1 limit")
    try:
        root = ElementTree.fromstring(xmp)
    except Exception as exc:
        raise MetadataExtractionError(f"malformed PDF XMP: {exc}") from exc
    xmp_fields = {
        "title": "title",
        "subject": "keywords",
        "creator": "author",
        "Keywords": "keywords",
        "CreatorTool": "creator_application",
        "Producer": "producer",
    }
    for native_name, field_name in xmp_fields.items():
        values = list(dict.fromkeys(_xml_texts(root, native_name)))
        if values:
            builder.add(
                MetadataSectionName.EMBEDDED,
                field_name,
                values if len(values) > 1 else values[0],
                source="pdf_xmp",
                source_type=MetadataSourceType.FORMAT_NATIVE,
                trust=MetadataTrustClass.EMBEDDED_DOCUMENT_METADATA,
            )
    for native_name, field_name in {
        "CreateDate": "embedded_created_at",
        "ModifyDate": "embedded_modified_at",
    }.items():
        for value in _xml_texts(root, native_name):
            builder.add_datetime(
                MetadataSectionName.EMBEDDED,
                field_name,
                value,
                source="pdf_xmp",
                source_type=MetadataSourceType.FORMAT_NATIVE,
                trust=MetadataTrustClass.EMBEDDED_DOCUMENT_METADATA,
            )


def _extract_image(path: Path, builder: _ProfileBuilder) -> None:
    try:
        from PIL import ExifTags, Image, IptcImagePlugin

        with Image.open(path) as image:
            exif = image.getexif()
            named = {ExifTags.TAGS.get(key, str(key)): value for key, value in exif.items()}
            iptc = IptcImagePlugin.getiptcinfo(image) or {}
            xmp = image.getxmp() if hasattr(image, "getxmp") else {}
    except Exception as exc:
        raise MetadataExtractionError(f"invalid image metadata: {exc}") from exc
    for native_name, field_name in {
        "Artist": "creator",
        "Software": "creator_application",
        "ImageDescription": "description",
        "Copyright": "copyright",
    }.items():
        builder.add(
            MetadataSectionName.EMBEDDED,
            field_name,
            named.get(native_name),
            source="image_exif",
            source_type=MetadataSourceType.FORMAT_NATIVE,
            trust=MetadataTrustClass.EMBEDDED_DOCUMENT_METADATA,
        )
    for native_name, field_name in {
        "DateTimeOriginal": "embedded_created_at",
        "DateTimeDigitized": "embedded_digitized_at",
        "DateTime": "embedded_modified_at",
    }.items():
        if named.get(native_name):
            builder.add_datetime(
                MetadataSectionName.EMBEDDED,
                field_name,
                str(named[native_name]).replace(":", "-", 2).replace(" ", "T", 1),
                source="image_exif",
                source_type=MetadataSourceType.FORMAT_NATIVE,
                trust=MetadataTrustClass.EMBEDDED_DOCUMENT_METADATA,
            )
    sensitive = []
    if "GPSInfo" in named:
        sensitive.append("gps")
    if any(key in named for key in ("BodySerialNumber", "CameraSerialNumber", "LensSerialNumber")):
        sensitive.append("device_serial")
    if sensitive:
        builder.add(
            MetadataSectionName.EMBEDDED,
            "sensitive_metadata_present",
            sensitive,
            source="image_exif",
            source_type=MetadataSourceType.FORMAT_NATIVE,
            trust=MetadataTrustClass.EMBEDDED_DOCUMENT_METADATA,
            status=MetadataNormalizationStatus.REDACTED_SENSITIVE,
        )
    iptc_fields = {
        (2, 5): "title",
        (2, 25): "keywords",
        (2, 80): "creator",
        (2, 105): "headline",
        (2, 120): "description",
    }
    for native_key, field_name in iptc_fields.items():
        raw_iptc: Any = iptc.get(native_key)
        value: Any = raw_iptc
        if isinstance(raw_iptc, bytes):
            value = raw_iptc.decode("utf-8", errors="replace")
        elif isinstance(raw_iptc, list):
            value = [
                item.decode("utf-8", errors="replace") if isinstance(item, bytes) else str(item)
                for item in raw_iptc
            ]
        builder.add(
            MetadataSectionName.EMBEDDED,
            field_name,
            value,
            source="image_iptc",
            source_type=MetadataSourceType.FORMAT_NATIVE,
            trust=MetadataTrustClass.EMBEDDED_DOCUMENT_METADATA,
        )
    if isinstance(xmp, dict):
        flattened = str(xmp)
        if len(flattened) <= MAX_METADATA_MEMBER_BYTES:
            for key, field_name in (
                ("CreatorTool", "creator_application"),
                ("CreateDate", "embedded_created_at"),
                ("ModifyDate", "embedded_modified_at"),
            ):
                match = re.search(rf"['\"]?{key}['\"]?\s*:\s*['\"]([^'\"]+)", flattened)
                if not match:
                    continue
                if key.endswith("Date"):
                    builder.add_datetime(
                        MetadataSectionName.EMBEDDED,
                        field_name,
                        match.group(1),
                        source="image_xmp",
                        source_type=MetadataSourceType.FORMAT_NATIVE,
                        trust=MetadataTrustClass.EMBEDDED_DOCUMENT_METADATA,
                    )
                else:
                    builder.add(
                        MetadataSectionName.EMBEDDED,
                        field_name,
                        match.group(1),
                        source="image_xmp",
                        source_type=MetadataSourceType.FORMAT_NATIVE,
                        trust=MetadataTrustClass.EMBEDDED_DOCUMENT_METADATA,
                    )


def _extract_eml(path: Path, builder: _ProfileBuilder) -> None:
    header = bytearray()
    with path.open("rb") as stream:
        while len(header) <= MAX_EMAIL_HEADERS_BYTES:
            line = stream.readline()
            if not line:
                break
            header.extend(line)
            if line in (b"\n", b"\r\n"):
                break
    if len(header) > MAX_EMAIL_HEADERS_BYTES:
        raise MetadataExtractionError("email headers exceed the v1 limit")
    try:
        message = BytesHeaderParser(policy=policy.default).parsebytes(bytes(header))
    except Exception as exc:
        raise MetadataExtractionError(f"malformed RFC 5322 headers: {exc}") from exc
    for native_name, field_name in {
        "Subject": "subject",
        "From": "email_sender",
        "Message-ID": "email_message_id",
    }.items():
        builder.add(
            MetadataSectionName.EMBEDDED,
            field_name,
            message.get(native_name),
            source="rfc5322_headers",
            source_type=MetadataSourceType.FORMAT_NATIVE,
            trust=MetadataTrustClass.EMBEDDED_DOCUMENT_METADATA,
        )
    if message.get("Date"):
        builder.add_datetime(
            MetadataSectionName.EMBEDDED,
            "embedded_sent_at",
            message.get("Date"),
            source="rfc5322_headers",
            source_type=MetadataSourceType.FORMAT_NATIVE,
            trust=MetadataTrustClass.EMBEDDED_DOCUMENT_METADATA,
            parser="rfc5322",
        )


def _add_context_facts(path: Path, builder: _ProfileBuilder) -> int:
    stat = path.stat()
    digest = hashlib.sha256()
    bytes_read = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            bytes_read += len(chunk)
            digest.update(chunk)
    identity = MetadataSectionName.IDENTITY
    builder.add(
        identity,
        "original_filename",
        builder.context.original_name,
        source="archive_mapping",
        source_type=MetadataSourceType.DERIVED,
        trust=MetadataTrustClass.DERIVED_METADATA,
    )
    builder.add(
        identity,
        "extension",
        path.suffix.lower(),
        source="archive_mapping",
        source_type=MetadataSourceType.DERIVED,
        trust=MetadataTrustClass.DERIVED_METADATA,
    )
    builder.add(
        identity,
        "mime_type",
        builder.context.mime_type
        or mimetypes.guess_type(builder.context.original_name)[0]
        or "application/octet-stream",
        source="archive_mapping",
        source_type=MetadataSourceType.DERIVED,
        trust=MetadataTrustClass.DERIVED_METADATA,
    )
    builder.add(
        identity,
        "size_bytes",
        stat.st_size,
        source="content_stream",
        source_type=MetadataSourceType.DERIVED,
        trust=MetadataTrustClass.DERIVED_METADATA,
    )
    builder.add(
        identity,
        "sha256",
        digest.hexdigest(),
        source="content_sha256",
        source_type=MetadataSourceType.DERIVED,
        trust=MetadataTrustClass.DERIVED_METADATA,
    )

    for field_name, value in (
        ("filesystem_birthtime", builder.context.filesystem_birthtime),
        ("filesystem_mtime", builder.context.filesystem_mtime),
        ("filesystem_ctime", builder.context.filesystem_ctime),
    ):
        if value is not None:
            builder.add_datetime(
                MetadataSectionName.FILESYSTEM,
                field_name,
                value.isoformat() if isinstance(value, datetime) else value,
                source="archive_registry_filesystem_metadata",
                source_type=MetadataSourceType.FILESYSTEM,
                trust=MetadataTrustClass.FILESYSTEM_METADATA,
            )
    if builder.context.filesystem_source_path:
        builder.add(
            MetadataSectionName.FILESYSTEM,
            "source_path",
            builder.context.filesystem_source_path,
            source="archive_registry_filesystem_metadata",
            source_type=MetadataSourceType.FILESYSTEM,
            trust=MetadataTrustClass.FILESYSTEM_METADATA,
        )

    builder.add(
        MetadataSectionName.ARCHIVE,
        "archive_source",
        builder.context.archive_source,
        source="archive_registry",
        source_type=MetadataSourceType.ARCHIVE_NATIVE,
        trust=MetadataTrustClass.ARCHIVE_SYSTEM,
    )
    builder.add(
        MetadataSectionName.ARCHIVE,
        "archive_object_id",
        builder.context.archive_object_id,
        source="archive_registry",
        source_type=MetadataSourceType.ARCHIVE_NATIVE,
        trust=MetadataTrustClass.ARCHIVE_SYSTEM,
    )
    builder.add(
        MetadataSectionName.ARCHIVE,
        "archive_original_name",
        builder.context.original_name,
        source="archive_registry",
        source_type=MetadataSourceType.ARCHIVE_NATIVE,
        trust=MetadataTrustClass.ARCHIVE_SYSTEM,
    )
    builder.add(
        MetadataSectionName.ARCHIVE,
        "archive_storage_locator",
        builder.context.archive_storage_locator,
        source="archive_registry",
        source_type=MetadataSourceType.ARCHIVE_NATIVE,
        trust=MetadataTrustClass.ARCHIVE_SYSTEM,
    )
    for field_name, value in (
        ("archived_at", builder.context.archived_at),
        ("archive_created_at", builder.context.archive_created_at),
        ("archive_modified_at", builder.context.archive_modified_at),
    ):
        if value is not None:
            builder.add_datetime(
                MetadataSectionName.ARCHIVE,
                field_name,
                value.isoformat() if isinstance(value, datetime) else value,
                source="archive_registry",
                source_type=MetadataSourceType.ARCHIVE_NATIVE,
                trust=MetadataTrustClass.ARCHIVE_SYSTEM,
            )
    if builder.context.parent_entity_ids:
        builder.add(
            MetadataSectionName.ARCHIVE,
            "parent_entity_ids",
            list(builder.context.parent_entity_ids),
            source="source_provenance_relations",
            source_type=MetadataSourceType.PARENT_CONTEXT,
            trust=MetadataTrustClass.ARCHIVE_SYSTEM,
        )
    if builder.context.ingested_at is not None:
        ingestion_value = builder.context.ingested_at
        builder.add_datetime(
            MetadataSectionName.INGESTION,
            "ingested_at",
            ingestion_value.isoformat()
            if isinstance(ingestion_value, datetime)
            else ingestion_value,
            source="indexed_document_registry",
            source_type=MetadataSourceType.INGESTION,
            trust=MetadataTrustClass.INGESTION_SYSTEM,
        )
    for field_name, fact_value in (
        ("extractor", DOCUMENT_METADATA_EXTRACTOR_NAME),
        ("extractor_version", DOCUMENT_METADATA_EXTRACTOR_VERSION),
        ("metadata_profile_id", DOCUMENT_METADATA_PROFILE_ID),
        ("metadata_profile_version", DOCUMENT_METADATA_PROFILE_VERSION),
        ("normalization_policy_id", DOCUMENT_METADATA_NORMALIZATION_ID),
        ("normalization_policy_version", DOCUMENT_METADATA_NORMALIZATION_VERSION),
        ("resolution_policy_id", METADATA_RESOLUTION_POLICY_ID),
        ("resolution_policy_version", METADATA_RESOLUTION_POLICY_VERSION),
    ):
        builder.add(
            MetadataSectionName.INGESTION,
            field_name,
            fact_value,
            source="metadata_backfill_job",
            source_type=MetadataSourceType.INGESTION,
            trust=MetadataTrustClass.INGESTION_SYSTEM,
        )
    return bytes_read


_NATIVE_EXTRACTORS: dict[str, tuple[str, Callable[[Path, _ProfileBuilder], None]]] = {
    ".pdf": ("PDF", _extract_pdf),
    ".docx": ("DOCX", _extract_ooxml),
    ".xlsx": ("XLSX", _extract_ooxml),
    ".pptx": ("PPTX", _extract_ooxml),
    ".odt": ("ODT", _extract_odf),
    ".ods": ("ODS", _extract_odf),
    ".odp": ("ODP", _extract_odf),
    ".png": ("IMAGE", _extract_image),
    ".jpg": ("IMAGE", _extract_image),
    ".jpeg": ("IMAGE", _extract_image),
    ".tif": ("IMAGE", _extract_image),
    ".tiff": ("IMAGE", _extract_image),
    ".webp": ("IMAGE", _extract_image),
    ".eml": ("EML", _extract_eml),
}
_IDENTITY_ONLY_FORMATS = {
    ".txt": "TXT",
    ".md": "TXT",
    ".csv": "CSV",
    ".html": "HTML",
    ".htm": "HTML",
    ".adoc": "TXT",
    ".asciidoc": "TXT",
    ".asc": "TXT",
}


def supported_metadata_formats() -> dict[str, str]:
    return {
        **{suffix: name for suffix, (name, _extractor) in _NATIVE_EXTRACTORS.items()},
        **_IDENTITY_ONLY_FORMATS,
    }


def extract_document_metadata(
    path: str | os.PathLike[str],
    context: ArchiveMetadataContext,
    *,
    extracted_at: datetime | None = None,
) -> ExtractionResult:
    """Extract only metadata; never parse content for chunks or embeddings."""
    import time

    started = time.perf_counter()
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise MetadataExtractionError("archived original must be a regular non-symlink file")
    if source.stat().st_size > MAX_SOURCE_FILE_BYTES:
        raise MetadataExtractionError("archived original exceeds the v1 one-GiB safety limit")
    suffix = Path(context.original_name).suffix.lower() or source.suffix.lower()
    builder = _ProfileBuilder(context, extracted_at or datetime.now(UTC))
    bytes_read = _add_context_facts(source, builder)
    native_supported = suffix in _NATIVE_EXTRACTORS
    if native_supported:
        format_name, extractor = _NATIVE_EXTRACTORS[suffix]
        extractor(source, builder)
    elif suffix in _IDENTITY_ONLY_FORMATS:
        format_name = _IDENTITY_ONLY_FORMATS[suffix]
        builder.warnings.append("format_has_no_v1_embedded_metadata_extractor")
    else:
        raise UnsupportedMetadataFormatError(f"unsupported metadata format: {suffix or '[none]'}")
    profile = builder.build()
    return ExtractionResult(
        profile=profile,
        format_name=format_name,
        bytes_read=bytes_read,
        elapsed_ms=(time.perf_counter() - started) * 1000,
        native_metadata_supported=native_supported,
        warnings=tuple(builder.warnings),
    )
