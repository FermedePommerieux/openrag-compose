"""Embedding model constants."""

import os

# Retrieval quality and recall are the product objective. Existing chunks keep
# their recorded model; this default only governs new selections/ingestions.
OPENAI_DEFAULT_EMBEDDING_MODEL = "text-embedding-3-large"
OPENAI_EMBEDDING_MODEL_PREFIX = "text-embedding"


def get_declared_default_embedding_model(provider: str) -> str:
    """Return the deployment-declared embedding default for a provider.

    A provider name can point at an internal gateway with a curated model
    catalog, so callers must not silently substitute a public-provider model.
    """
    declared_provider = os.environ.get("EMBEDDING_PROVIDER", "")
    declared_model = os.environ.get("EMBEDDING_MODEL", "")
    if declared_provider == provider and declared_model:
        return declared_model
    return ""
