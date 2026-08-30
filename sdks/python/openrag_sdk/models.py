"""OpenRAG SDK data models."""

from typing import Literal

from pydantic import BaseModel, Field


# Chat models
class Source(BaseModel):
    """A source document returned in chat/search results."""

    filename: str
    text: str
    score: float | None
    page: int | None = None
    mimetype: str | None = None
    source_url: str | None = None
    document_id: str | None = None
    chunk_id: str | None = None
    chunk_index: int | None = None
    chunking_strategy: str | None = None
    connector_file_id: str | None = None
    chunk_content_sha256: str | None = None
    document_content_sha256: str | None = None
    evidence_order: int | None = None
    source_provenance: dict | None = None
    source_entity_id: str | None = None
    source_entity_type: str | None = None
    source_entity_system: str | None = None
    source_entity_alternate_ids: list[str] = Field(default_factory=list)
    source_relation_target_ids: list[str] = Field(default_factory=list)
    source_relation_roles: list[str] = Field(default_factory=list)
    source_relative_path: str | None = None
    source_path_ancestors: list[str] = Field(default_factory=list)


class EvidenceCoverage(BaseModel):
    """Machine-verifiable progress for a document or documentary scope."""

    mode: Literal["exhaustive", "scope_exhaustive"] = "exhaustive"
    document_id: str | None = None
    snapshot_sha256: str | None = None
    covered_chunks: int
    total_chunks: int
    coverage_ratio: float | None = None
    document_read_coverage_ratio: float | None = None
    complete: bool
    next_cursor: str | None = None
    query: str | None = None
    seed_discovery_complete: bool | None = None
    seed_documents: int | None = None
    seed_entities: int | None = None
    valid_provenance_seed_documents: int | None = None
    invalid_provenance_seed_documents: int | None = None
    seed_provenance_complete: bool | None = None
    graph_entities_visited: int | None = None
    graph_frontier_empty: bool | None = None
    graph_limit_reached: bool | None = None
    graph_stop_reason: str | None = None
    documents_discovered: int | None = None
    documents_complete: int | None = None
    documents_incomplete: int | None = None
    stop_reason: str | None = None
    status_code: str | None = None
    status_message: str | None = None
    failure_codes: list[str] = Field(default_factory=list)
    model_evidence_chunks: int | None = None
    artifact_chunks: int | None = None


class ChatResponse(BaseModel):
    """Response from a non-streaming chat request."""

    response: str
    chat_id: str | None = None
    sources: list[Source] = Field(default_factory=list)


class StreamEvent(BaseModel):
    """Base class for streaming events."""

    type: Literal["content", "sources", "done"]


class ContentEvent(StreamEvent):
    """A content delta event during streaming."""

    type: Literal["content"] = "content"
    delta: str


class SourcesEvent(StreamEvent):
    """A sources event containing retrieved documents."""

    type: Literal["sources"] = "sources"
    sources: list[Source]


class DoneEvent(StreamEvent):
    """Indicates the stream is complete."""

    type: Literal["done"] = "done"
    chat_id: str | None = None


# Search models
class SearchResult(BaseModel):
    """A focused match or one leaf chunk from an exhaustive evidence read."""

    filename: str
    text: str
    score: float | None
    page: int | None = None
    mimetype: str | None = None
    source_url: str | None = None
    document_id: str | None = None
    chunk_id: str | None = None
    chunk_index: int | None = None
    chunking_strategy: str | None = None
    connector_file_id: str | None = None
    chunk_content_sha256: str | None = None
    document_content_sha256: str | None = None
    evidence_order: int | None = None
    source_provenance: dict | None = None
    source_entity_id: str | None = None
    source_entity_type: str | None = None
    source_entity_system: str | None = None
    source_entity_alternate_ids: list[str] = Field(default_factory=list)
    source_relation_target_ids: list[str] = Field(default_factory=list)
    source_relation_roles: list[str] = Field(default_factory=list)
    source_relative_path: str | None = None
    source_path_ancestors: list[str] = Field(default_factory=list)


class SearchResponse(BaseModel):
    """Response from a search request."""

    results: list[SearchResult]
    coverage: EvidenceCoverage | None = None
    error: str | None = None
    documents: list[dict] = Field(default_factory=list)
    evidence_batches: list[dict] = Field(default_factory=list)
    graph: dict | None = None


# Document models
class IngestResponse(BaseModel):
    """Response from document ingestion (async task-based)."""

    task_id: str
    status: str | None = None  # Optional - we poll for actual status
    filename: str | None = None


class IngestTaskStatus(BaseModel):
    """Status of an ingestion task."""

    task_id: str
    status: str  # "pending", "running", "completed", "failed"
    total_files: int = 0
    processed_files: int = 0
    successful_files: int = 0
    failed_files: int = 0
    files: dict = {}  # Detailed per-file status


class DeleteDocumentResponse(BaseModel):
    """Response from document deletion."""

    success: bool
    deleted_chunks: int = 0
    filename: str | None = None
    message: str | None = None
    error: str | None = None
    # Populated when deleting by filter_id — one entry per resolved data_source.
    filenames: list[str] | None = None
    filter_id: str | None = None
    per_file: list[dict] | None = None


# Chat history models
class Message(BaseModel):
    """A message in a conversation."""

    role: str
    content: str
    timestamp: str | None = None


class Conversation(BaseModel):
    """A conversation summary."""

    chat_id: str
    title: str = ""
    created_at: str | None = None
    last_activity: str | None = None
    message_count: int = 0


class ConversationDetail(Conversation):
    """A conversation with full message history."""

    messages: list[Message] = Field(default_factory=list)


class ConversationListResponse(BaseModel):
    """Response from listing conversations."""

    conversations: list[Conversation]


# Settings models
class AgentSettings(BaseModel):
    """Agent configuration settings."""

    llm_provider: str | None = None
    llm_model: str | None = None
    system_prompt: str | None = None


class KnowledgeSettings(BaseModel):
    """Knowledge base configuration settings."""

    embedding_provider: str | None = None
    embedding_model: str | None = None
    chunk_size: int | None = None
    chunk_overlap: int | None = None
    table_structure: bool | None = None
    ocr: bool | None = None
    ocr_mojibake_fallback: bool | None = None
    picture_descriptions: bool | None = None
    retrieval_scope_seed_count: int | None = None
    retrieval_scope_max_depth: int | None = None
    retrieval_scope_max_entities: int | None = None
    retrieval_scope_max_documents: int | None = None
    retrieval_scope_batch_size: int | None = None


class ArchivingSettings(BaseModel):
    """Source archiving configuration settings."""

    available: bool = False
    enabled: bool = False


class SettingsResponse(BaseModel):
    """Response from settings endpoint."""

    agent: AgentSettings = Field(default_factory=AgentSettings)
    knowledge: KnowledgeSettings = Field(default_factory=KnowledgeSettings)
    archiving: ArchivingSettings = Field(default_factory=ArchivingSettings)


# Request models
class SearchFilters(BaseModel):
    """Filters for search requests."""

    data_sources: list[str] | None = None
    document_types: list[str] | None = None


# Settings update models
class SettingsUpdateOptions(BaseModel):
    """Options for updating settings."""

    llm_model: str | None = None
    llm_provider: str | None = None
    system_prompt: str | None = None
    embedding_model: str | None = None
    embedding_provider: str | None = None
    chunk_size: int | None = None
    chunk_overlap: int | None = None
    table_structure: bool | None = None
    ocr: bool | None = None
    ocr_mojibake_fallback: bool | None = None
    picture_descriptions: bool | None = None
    archive_sources_enabled: bool | None = None
    retrieval_scope_seed_count: int | None = None
    retrieval_scope_max_depth: int | None = None
    retrieval_scope_max_entities: int | None = None
    retrieval_scope_max_documents: int | None = None
    retrieval_scope_batch_size: int | None = None


class SettingsUpdateResponse(BaseModel):
    """Response from settings update."""

    message: str


# Models (list available LLM/embedding models per provider)
class ModelOption(BaseModel):
    """A single model option returned by the models list endpoint."""

    value: str
    label: str
    default: bool = False


class ModelsResponse(BaseModel):
    """Response from listing models for a provider."""

    language_models: list[ModelOption] = Field(default_factory=list)
    embedding_models: list[ModelOption] = Field(default_factory=list)


# Knowledge filter models
class KnowledgeFilterQueryData(BaseModel):
    """Query configuration stored in a knowledge filter."""

    query: str | None = None
    filters: dict[str, list[str]] | None = None
    limit: int | None = None
    score_threshold: float | None = Field(default=None, alias="scoreThreshold")
    color: str | None = None
    icon: str | None = None

    model_config = {"populate_by_name": True}


class KnowledgeFilter(BaseModel):
    """A knowledge filter definition."""

    id: str
    name: str
    description: str | None = None
    query_data: KnowledgeFilterQueryData | None = Field(default=None, alias="queryData")
    owner: str | None = None
    created_at: str | None = Field(default=None, alias="createdAt")
    updated_at: str | None = Field(default=None, alias="updatedAt")

    model_config = {"populate_by_name": True}


class CreateKnowledgeFilterOptions(BaseModel):
    """Options for creating a knowledge filter."""

    name: str
    description: str | None = None
    query_data: KnowledgeFilterQueryData = Field(alias="queryData")

    model_config = {"populate_by_name": True}


class UpdateKnowledgeFilterOptions(BaseModel):
    """Options for updating a knowledge filter."""

    name: str | None = None
    description: str | None = None
    query_data: KnowledgeFilterQueryData | None = Field(default=None, alias="queryData")

    model_config = {"populate_by_name": True}


class CreateKnowledgeFilterResponse(BaseModel):
    """Response from creating a knowledge filter."""

    success: bool
    id: str | None = None
    error: str | None = None


class KnowledgeFilterSearchResponse(BaseModel):
    """Response from searching knowledge filters."""

    success: bool
    filters: list[KnowledgeFilter] = Field(default_factory=list)


class GetKnowledgeFilterResponse(BaseModel):
    """Response from getting a knowledge filter."""

    success: bool
    filter: KnowledgeFilter | None = None
    error: str | None = None


class DeleteKnowledgeFilterResponse(BaseModel):
    """Response from deleting a knowledge filter."""

    success: bool
    error: str | None = None
