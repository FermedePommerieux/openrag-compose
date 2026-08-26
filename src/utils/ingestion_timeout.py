"""Deterministic per-document ingestion timeout budgets.

An absolute wall-clock limit that is identical for every document is not a
valid safety boundary: a scanned 100+ page PDF can remain healthy long after a
small office document should have completed.  File size is available before
Docling submission on every local/upload path, so it provides a conservative,
auditable first-pass complexity signal without parsing the document twice.
"""

import math
import os
from typing import Any

MEBIBYTE = 1024 * 1024


def local_file_size_bytes(item: Any) -> int | None:
    """Return a local file's size, or ``None`` for connector/non-file items."""
    try:
        path = os.fspath(item)
    except TypeError:
        return None
    try:
        if not os.path.isfile(path):
            return None
        return max(0, os.path.getsize(path))
    except OSError:
        return None


def adaptive_ingestion_timeout_seconds(
    *,
    base_seconds: int,
    file_size_bytes: int | None,
    seconds_per_mib: int,
    max_seconds: int,
) -> int:
    """Return a bounded timeout budget derived from known document size.

    ``base_seconds`` is always the minimum. Each started MiB adds the configured
    allowance, and ``max_seconds`` bounds genuinely stuck work. Unknown-size
    connector items retain the base budget.
    """
    base = max(1, int(base_seconds))
    ceiling = max(base, int(max_seconds))
    per_mib = max(0, int(seconds_per_mib))
    if file_size_bytes is None or file_size_bytes <= 0 or per_mib == 0:
        return base

    size_mib = math.ceil(file_size_bytes / MEBIBYTE)
    return min(ceiling, base + size_mib * per_mib)
