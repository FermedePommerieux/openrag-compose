"""Centralized path helpers for OpenRAG.

Archive-related paths are exposed by ``config.settings``. The documents helper
below remains as a compatibility wrapper for callers not yet migrated.
"""

import os
import re
from urllib.parse import quote


# ---------------------------------------------------------------------------
# Documents directory
# ---------------------------------------------------------------------------
def get_documents_path() -> str:
    """Return the path to the documents directory.

    Environment variable: OPENRAG_DOCUMENTS_PATH
    Default: ``openrag-documents``  (relative to the working directory)
    """
    from config.settings import get_documents_path as get_configured_documents_path

    return get_configured_documents_path()


# ---------------------------------------------------------------------------
# JWT keys directory
# ---------------------------------------------------------------------------
def get_keys_path() -> str:
    """Return the path to the JWT keys directory.

    Environment variable: OPENRAG_KEYS_PATH
    Default: ``keys``  (relative to the working directory)
    """
    return os.getenv("OPENRAG_KEYS_PATH") or "keys"


# ---------------------------------------------------------------------------
# Flows directory
# ---------------------------------------------------------------------------
def get_flows_path() -> str:
    """Return the path to the flows directory.

    Environment variable: OPENRAG_FLOWS_PATH
    Default: ``flows``  (relative to the working directory)
    """
    return os.getenv("OPENRAG_FLOWS_PATH") or "flows"


def get_flows_backup_path() -> str:
    """Return the path to the flows backup directory.

    Environment variable: OPENRAG_FLOWS_BACKUP_PATH
    Default: ``<flows_path>/backup``
    """
    return os.getenv("OPENRAG_FLOWS_BACKUP_PATH") or os.path.join(get_flows_path(), "backup")


def get_flows_source_metadata() -> dict[str, str] | None:
    """Return the immutable repository provenance for installed core flows.

    The update dialog resets persisted Langflow graphs from ``OPENRAG_FLOWS_PATH``;
    it does *not* update the Langflow application.  A deployment that downloads
    those files from Git should set all three variables below so operators can
    verify exactly which release source the dialog will apply:

    - ``OPENRAG_FLOWS_SOURCE_REPOSITORY``: GitHub ``owner/repository`` slug
    - ``OPENRAG_FLOWS_SOURCE_BRANCH``: human-readable published release branch
    - ``OPENRAG_FLOWS_SOURCE_REVISION``: immutable 40-character commit SHA

    Incomplete or malformed provenance fails closed to ``None``: the UI then
    describes the source as local installed files and never invents an upstream
    repository.  The flow update itself remains available for ordinary local
    OpenRAG installations.
    """
    repository = os.getenv("OPENRAG_FLOWS_SOURCE_REPOSITORY", "").strip()
    branch = os.getenv("OPENRAG_FLOWS_SOURCE_BRANCH", "").strip()
    revision = os.getenv("OPENRAG_FLOWS_SOURCE_REVISION", "").strip().lower()

    slug_pattern = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
    if not re.fullmatch(slug_pattern, repository):
        return None
    if not branch or any(character in branch for character in "\r\n"):
        return None
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        return None

    repository_url = f"https://github.com/{repository}"
    return {
        "repository": repository,
        "branch": branch,
        "revision": revision,
        "branch_url": f"{repository_url}/tree/{quote(branch, safe='/')}",
        "revision_url": f"{repository_url}/tree/{revision}/flows",
    }


# ---------------------------------------------------------------------------
# Config directory (holds config.yaml)
# ---------------------------------------------------------------------------
def get_config_path() -> str:
    """Return the path to the configuration directory.

    Environment variable: OPENRAG_CONFIG_PATH
    Default: ``config``  (relative to the working directory)
    """
    return os.getenv("OPENRAG_CONFIG_PATH") or "config"


def get_config_file_path() -> str:
    """Return the full path to the config.yaml file."""
    return os.path.join(get_config_path(), "config.yaml")


# ---------------------------------------------------------------------------
# Data directory (conversations, tokens, connections, etc.)
# ---------------------------------------------------------------------------
def get_data_path() -> str:
    """Return the path to the data directory.

    Environment variable: OPENRAG_DATA_PATH
    Default: ``data``  (relative to the working directory)
    """
    return os.getenv("OPENRAG_DATA_PATH") or "data"


def get_data_file(filename: str) -> str:
    """Return a full path for a file inside the data directory.

    Example::

        get_data_file("conversations.json")
        # → "data/conversations.json"  (or $OPENRAG_DATA_PATH/conversations.json)
    """
    return os.path.join(get_data_path(), filename)
