"""Deterministic, fail-closed natural-language metadata planning.

This parser intentionally recognizes only narrow French/English surface forms.
Anything outside that subset remains semantic free text or becomes an explicit
AMBIGUOUS/UNSUPPORTED result; no model is called and no query DSL is emitted.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from models.document_investigation import CalendarBasis
from models.metadata_agent_search import (
    MAX_AGENT_FILTERS,
    MAX_AGENT_FREE_TEXT_LENGTH,
    MetadataAgentFilter,
    MetadataAgentOperator,
    MetadataPlanStatus,
    NaturalLanguageMetadataPlan,
)
from models.metadata_filter import MetadataFilterField

_MONTHS = {
    "janvier": 1,
    "january": 1,
    "février": 2,
    "fevrier": 2,
    "february": 2,
    "mars": 3,
    "march": 3,
    "avril": 4,
    "april": 4,
    "mai": 5,
    "may": 5,
    "juin": 6,
    "june": 6,
    "juillet": 7,
    "july": 7,
    "août": 8,
    "aout": 8,
    "august": 8,
    "septembre": 9,
    "september": 9,
    "octobre": 10,
    "october": 10,
    "novembre": 11,
    "november": 11,
    "décembre": 12,
    "decembre": 12,
    "december": 12,
}
_MONTH_PATTERN = "|".join(sorted((re.escape(value) for value in _MONTHS), key=len, reverse=True))
_MONTH_YEAR_RE = re.compile(
    rf"\b(?P<month>{_MONTH_PATTERN})\s+(?P<year>(?:19|20)\d{{2}})\b",
    re.IGNORECASE,
)
_BARE_MONTH_RE = re.compile(
    rf"\b(?:en|de|du|in|from)\s+(?P<month>{_MONTH_PATTERN})\b(?!\s+(?:19|20)\d{{2}})",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"\b(?:en|de|in|from)\s+(?P<year>(?:19|20)\d{2})\b", re.IGNORECASE)
_UTC_RE = re.compile(r"\bUTC\b", re.IGNORECASE)

_FORMAT_RULES: tuple[tuple[re.Pattern[str], MetadataFilterField, str], ...] = (
    (re.compile(r"\bPDFs?\b", re.IGNORECASE), MetadataFilterField.FORMAT_FAMILY, "pdf"),
    (re.compile(r"\bXLSX\b", re.IGNORECASE), MetadataFilterField.EXTENSION, "xlsx"),
    (re.compile(r"\bXLS\b", re.IGNORECASE), MetadataFilterField.EXTENSION, "xls"),
    (
        re.compile(r"\b(?:fichiers?\s+)?Excel\b", re.IGNORECASE),
        MetadataFilterField.FORMAT_FAMILY,
        "spreadsheet",
    ),
    (re.compile(r"\bDOCX\b", re.IGNORECASE), MetadataFilterField.EXTENSION, "docx"),
    (
        re.compile(r"\b(?:documents?\s+)?Word\b", re.IGNORECASE),
        MetadataFilterField.FORMAT_FAMILY,
        "text_document",
    ),
    (re.compile(r"\bPPTX\b", re.IGNORECASE), MetadataFilterField.EXTENSION, "pptx"),
    (
        re.compile(r"\bPowerPoint\b", re.IGNORECASE),
        MetadataFilterField.FORMAT_FAMILY,
        "presentation",
    ),
    (re.compile(r"\bCSV\b", re.IGNORECASE), MetadataFilterField.EXTENSION, "csv"),
)


def _mark(mask: list[bool], start: int, end: int) -> None:
    for offset in range(max(0, start), min(len(mask), end)):
        mask[offset] = True


def _masked_text(text: str, mask: list[bool]) -> str:
    value = "".join(
        " " if hidden else character
        for character, hidden in zip(text, mask, strict=True)
    )
    value = re.sub(r"[\s,;:]+", " ", value).strip(" .,-;:")
    value = re.sub(r"^(?:les?|la|des?|the|some)\b\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(
        r"^(?:documents?|fichiers?|files?)\b\s*", "", value, flags=re.IGNORECASE
    )
    value = re.sub(
        r"^(?:qui\s+)?(?:contien(?:t|nent)|contenant|containing|about)\b\s*",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"\b(?:qui\s+)?(?:contien(?:t|nent)|contenant|containing)\b\s*",
        " ",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"\s+", " ", value).strip(" .,-;:")
    return value


def _date_role(text: str, start: int) -> str:
    prefix = text[max(0, start - 64) : start].lower()
    if re.search(r"modifi(?:é|ée|és|ées|e|ed|cation)?\s*(?:en|in)?\s*$", prefix):
        return "modification"
    if re.search(
        r"(?:produit|produite|produits|produites|créé|créée|créés|créées|cree|"
        r"created|produced|generated)\s*(?:en|in)?\s*$",
        prefix,
    ):
        return "production"
    return "production"


def _date_field(role: str, granularity: str) -> MetadataFilterField:
    return MetadataFilterField(f"{role}_{granularity}")


def _deduplicate(filters: Iterable[MetadataAgentFilter]) -> tuple[MetadataAgentFilter, ...]:
    unique: dict[str, MetadataAgentFilter] = {}
    for item in filters:
        key = repr(item.canonical_payload())
        unique[key] = item
    return tuple(unique.values())


def _explicit_overrides(
    inferred: Iterable[MetadataAgentFilter],
    explicit: tuple[MetadataAgentFilter, ...],
) -> tuple[MetadataAgentFilter, ...]:
    """Preserve explicit predicates byte-for-byte and discard guesses on their fields."""
    explicit_fields = {item.field for item in explicit}
    return _deduplicate(
        [item for item in inferred if item.field not in explicit_fields] + list(explicit)
    )


def plan_metadata_query(
    natural_language: str,
    *,
    explicit_filters: tuple[MetadataAgentFilter, ...] = (),
) -> NaturalLanguageMetadataPlan:
    """Return a deterministic plan without executing retrieval."""
    text = str(natural_language or "").strip()
    if not text:
        return NaturalLanguageMetadataPlan(
            status=MetadataPlanStatus.INVALID,
            unsupported_constraints=("empty_query",),
        )
    if len(text) > MAX_AGENT_FREE_TEXT_LENGTH:
        return NaturalLanguageMetadataPlan(
            status=MetadataPlanStatus.INVALID,
            unsupported_constraints=("query_too_long",),
            metadata_intent_detected=bool(explicit_filters),
        )

    mask = [False] * len(text)
    inferred: list[MetadataAgentFilter] = []
    ambiguities: list[str] = []
    unsupported: list[str] = []
    metadata_intent = bool(explicit_filters)

    if re.search(r"\b(?:pi[eè]ces?\s+jointes?|attachments?)\s+(?:de|of)\s+(?:ce|this)\s+mail\b", text, re.IGNORECASE):
        metadata_intent = True
        unsupported.append("implicit_parent_source_identity")

    if re.search(r"\b(?:archiv\w*|ingest\w*)\b", text, re.IGNORECASE) and (
        _MONTH_YEAR_RE.search(text) or _YEAR_RE.search(text)
    ):
        metadata_intent = True
        unsupported.append("archive_or_ingestion_calendar")

    if re.search(r"\b(?:avant|apr[eè]s|before|after)\b", text, re.IGNORECASE) and (
        _MONTH_YEAR_RE.search(text) or _YEAR_RE.search(text)
    ):
        metadata_intent = True
        unsupported.append("natural_language_date_range")

    if re.search(
        r"\b(?:cr[ée][ée]\w*|created)\s+(?:ou|or)\s+(?:modifi\w*|modified)\b",
        text,
        re.IGNORECASE,
    ):
        metadata_intent = True
        unsupported.append("disjunctive_temporal_role")

    format_matches: list[tuple[re.Match[str], MetadataFilterField, str]] = []
    for pattern, field, value in _FORMAT_RULES:
        format_matches.extend((match, field, value) for match in pattern.finditer(text))
    format_matches.sort(key=lambda item: item[0].start())
    if len(format_matches) > 1:
        for left, right in zip(format_matches, format_matches[1:], strict=False):
            between = text[left[0].end() : right[0].start()]
            if re.search(r"\b(?:ou|or)\b", between, re.IGNORECASE):
                metadata_intent = True
                unsupported.append("disjunctive_format")
                break

    for match, field, value in format_matches:
        metadata_intent = True
        prefix = text[max(0, match.start() - 12) : match.start()]
        negated = bool(
            re.search(r"(?:pas\s+(?:les?|des?)?|not|excluding|sans)\s*$", prefix, re.IGNORECASE)
        )
        inferred.append(
            MetadataAgentFilter(
                field=field,
                operator=(MetadataAgentOperator.NOT_EQUAL if negated else MetadataAgentOperator.EQUAL),
                value=value,
            )
        )
        start = match.start()
        if negated:
            negation = re.search(
                r"(?:pas\s+(?:les?|des?)?|not|excluding|sans)\s*$",
                text[max(0, start - 20) : start],
                re.IGNORECASE,
            )
            if negation:
                start = max(0, start - 20) + negation.start()
        _mark(mask, start, match.end())

    month_matches = list(_MONTH_YEAR_RE.finditer(text))
    if len(month_matches) > 1:
        for left_date, right_date in zip(month_matches, month_matches[1:], strict=False):
            if re.search(
                r"\b(?:ou|or)\b",
                text[left_date.end() : right_date.start()],
                re.IGNORECASE,
            ):
                unsupported.append("disjunctive_calendar")
                break
    consumed_date_spans: list[tuple[int, int]] = []
    for match in month_matches:
        metadata_intent = True
        role = _date_role(text, match.start())
        month = _MONTHS[match.group("month").lower()]
        basis = CalendarBasis.UTC if _UTC_RE.search(text[match.end() : match.end() + 8]) else CalendarBasis.SOURCE_LOCAL
        inferred.append(
            MetadataAgentFilter(
                field=_date_field(role, "month"),
                operator=MetadataAgentOperator.EQUAL,
                value=f"{match.group('year')}-{month:02d}",
                calendar_basis=basis,
            )
        )
        start = match.start()
        prefix = text[max(0, start - 28) : start]
        cue = re.search(
            r"(?:produit\w*|cr[ée][ée]\w*|modifi\w*|created|produced|generated|modified)\s+(?:en|in)\s*$",
            prefix,
            re.IGNORECASE,
        )
        if cue:
            start = max(0, start - 28) + cue.start()
        else:
            preposition = re.search(r"(?:de|du|en|in|from)\s*$", prefix, re.IGNORECASE)
            if preposition:
                start = max(0, start - 28) + preposition.start()
        end = match.end()
        utc = _UTC_RE.match(text[end : end + 8].lstrip())
        if utc:
            end += len(text[end : end + 8]) - len(text[end : end + 8].lstrip()) + utc.end()
        _mark(mask, start, end)
        consumed_date_spans.append((match.start(), match.end()))

    for match in _YEAR_RE.finditer(text):
        if any(start <= match.start() < end for start, end in consumed_date_spans):
            continue
        metadata_intent = True
        role = _date_role(text, match.start())
        basis = CalendarBasis.UTC if _UTC_RE.search(text[match.end() : match.end() + 8]) else CalendarBasis.SOURCE_LOCAL
        inferred.append(
            MetadataAgentFilter(
                field=_date_field(role, "year"),
                operator=MetadataAgentOperator.EQUAL,
                value=match.group("year"),
                calendar_basis=basis,
            )
        )
        start = match.start()
        prefix = text[max(0, start - 28) : start]
        cue = re.search(
            r"(?:produit\w*|cr[ée][ée]\w*|modifi\w*|created|produced|generated|modified)\s*$",
            prefix,
            re.IGNORECASE,
        )
        if cue:
            start = max(0, start - 28) + cue.start()
        end = match.end()
        utc_suffix = text[end : end + 8]
        utc = _UTC_RE.match(utc_suffix.lstrip())
        if utc:
            end += len(utc_suffix) - len(utc_suffix.lstrip()) + utc.end()
        _mark(mask, start, end)

    for _match in _BARE_MONTH_RE.finditer(text):
        metadata_intent = True
        ambiguities.append("calendar_month_without_year")

    creator_exists = re.search(
        r"\b(?:avec\s+un\s+cr[ée]ateur\s+renseign[ée]|with\s+(?:a\s+)?creator\s+(?:set|present))\b",
        text,
        re.IGNORECASE,
    )
    if creator_exists:
        metadata_intent = True
        inferred.append(
            MetadataAgentFilter(
                field=MetadataFilterField.CREATOR_OBSERVATION,
                operator=MetadataAgentOperator.EXISTS,
            )
        )
        _mark(mask, creator_exists.start(), creator_exists.end())

    creator_exact = re.search(
        r"\b(?:cr[ée][ée]s?|created)\s+par\s+(?P<name>[\wÀ-ÖØ-öø-ÿ' -]{2,80}?)(?=$|\s+(?:en|de|du|avec|with|contenant|containing)\b)",
        text,
        re.IGNORECASE,
    ) or re.search(
        r"\bcreated\s+by\s+(?P<name>[\wÀ-ÖØ-öø-ÿ' -]{2,80}?)(?=$|\s+(?:in|from|with|containing)\b)",
        text,
        re.IGNORECASE,
    )
    if creator_exact:
        metadata_intent = True
        inferred.append(
            MetadataAgentFilter(
                field=MetadataFilterField.CREATOR_OBSERVATION,
                operator=MetadataAgentOperator.EQUAL,
                value=creator_exact.group("name").strip(),
            )
        )
        _mark(mask, creator_exact.start(), creator_exact.end())

    openarchiver = re.search(r"\bOpenArchiver\b", text, re.IGNORECASE)
    if openarchiver and (
        re.search(r"\b(?:depuis|provenant\s+de|from|source)\b", text, re.IGNORECASE)
        or re.search(r"\b(?:documents?|fichiers?|files?)\b", text, re.IGNORECASE)
    ):
        metadata_intent = True
        inferred.append(
            MetadataAgentFilter(
                field=MetadataFilterField.SOURCE_SYSTEM,
                operator=MetadataAgentOperator.EQUAL,
                value="openarchiver",
            )
        )
        start = openarchiver.start()
        prefix = text[max(0, start - 20) : start]
        source_cue = re.search(r"(?:depuis|provenant\s+de|from|source)\s*$", prefix, re.IGNORECASE)
        if source_cue:
            start = max(0, start - 20) + source_cue.start()
        _mark(mask, start, openarchiver.end())

    effective = _explicit_overrides(inferred, explicit_filters)
    if len(effective) > MAX_AGENT_FILTERS:
        return NaturalLanguageMetadataPlan(
            status=MetadataPlanStatus.INVALID,
            unsupported_constraints=("too_many_filters",),
            metadata_intent_detected=True,
        )

    free_text = _masked_text(text, mask) if effective else text
    if effective and not free_text:
        # The validated search path intentionally rejects filter-only partial
        # retrieval. Retain a neutral noun already implied by file/format-only
        # requests so q1 still ranks only inside the eligible occurrence set.
        free_text = "documents"

    unsupported = list(dict.fromkeys(unsupported))[:4]
    ambiguities = list(dict.fromkeys(ambiguities))[:4]
    if unsupported:
        status = MetadataPlanStatus.UNSUPPORTED
    elif ambiguities:
        status = MetadataPlanStatus.AMBIGUOUS
    else:
        status = MetadataPlanStatus.VALID

    return NaturalLanguageMetadataPlan(
        status=status,
        free_text=free_text,
        filters=effective,
        ambiguities=tuple(ambiguities),
        unsupported_constraints=tuple(unsupported),
        metadata_intent_detected=metadata_intent,
        requires_metadata_search=status == MetadataPlanStatus.VALID and bool(effective),
    )
