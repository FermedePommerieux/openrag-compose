"""Pydantic request and response models for the settings/onboarding endpoints.

Lifted verbatim from the original `src/api/settings.py` (lines 49–223). No
shape changes — every external caller sees the same fields and validators
they did before. See `src/api/settings/__init__.py` for re-exports.
"""

from typing import Any

from pydantic import BaseModel, Field, field_validator

from services.docling_service import DoclingConfig


class SettingsUpdateBody(BaseModel):
    llm_model: str | None = Field(None, min_length=1)
    llm_provider: str | None = Field(None, pattern="^(openai|anthropic|watsonx|ollama)$")
    system_prompt: str | None = None
    chunk_size: int | None = Field(None, gt=0)
    chunk_overlap: int | None = Field(None, ge=0)
    chunking_strategy: str | None = Field(None, pattern="^(character|hybrid)$")
    hybrid_max_tokens: int | None = Field(None, gt=0)
    hybrid_merge_peers: bool | None = None
    table_structure: bool | None = None
    ocr: bool | None = None
    picture_descriptions: bool | None = None
    disable_ingest_with_langflow: bool | None = None
    archive_sources_enabled: bool | None = None
    embedding_model: str | None = Field(None, min_length=1)
    embedding_provider: str | None = Field(None, pattern="^(openai|watsonx|ollama)$")
    index_name: str | None = Field(None, min_length=1)
    retrieval_strategy: str | None = Field(None, pattern="^(weighted|rrf)$")
    retrieval_mode: str | None = Field(None, pattern="^(hybrid|lexical|vector)$")
    retrieval_lexical_candidates: int | None = Field(None, gt=0, le=500)
    retrieval_vector_candidates: int | None = Field(None, gt=0, le=500)
    retrieval_rrf_k: int | None = Field(None, gt=0, le=1000)
    retrieval_max_chunks_per_document: int | None = Field(None, gt=0, le=100)
    retrieval_adaptive_max_chunks_per_document: int | None = Field(None, gt=0, le=100)
    retrieval_reranker_url: str | None = Field(None, max_length=2048)
    retrieval_reranker_timeout: int | None = Field(None, gt=0, le=120)
    retrieval_debug: bool | None = None
    openai_api_key: str | None = Field(None, min_length=1)
    anthropic_api_key: str | None = Field(None, min_length=1)
    watsonx_api_key: str | None = Field(None, min_length=1)
    watsonx_endpoint: str | None = Field(None, min_length=1)
    watsonx_project_id: str | None = Field(None, min_length=1)
    ollama_endpoint: str | None = Field(None, min_length=1)
    remove_ollama_config: bool | None = None
    remove_openai_config: bool | None = None
    remove_anthropic_config: bool | None = None
    remove_watsonx_config: bool | None = None
    # Explicit confirmation that the caller accepts removing a provider whose
    # embedding models are still in use by indexed documents. Without this,
    # the backend returns 409 and the frontend prompts the user.
    force_remove: bool | None = False


class OnboardingBody(BaseModel):
    llm_provider: str | None = Field(None, pattern="^(openai|anthropic|watsonx|ollama)$")
    llm_model: str | None = Field(None, min_length=1)
    embedding_provider: str | None = Field(None, pattern="^(openai|watsonx|ollama)$")
    embedding_model: str | None = Field(None, min_length=1)
    openai_api_key: str | None = Field(None, min_length=1)
    anthropic_api_key: str | None = Field(None, min_length=1)
    watsonx_api_key: str | None = Field(None, min_length=1)
    watsonx_endpoint: str | None = Field(None, min_length=1)
    watsonx_project_id: str | None = Field(None, min_length=1)
    ollama_endpoint: str | None = Field(None, min_length=1)


class CitationDisplayData(BaseModel):
    file_path: str | None = None
    source_url: str | None = None
    page: int | str | None = None
    score: float | str | None = None
    text: str | None = None
    embedding_model: str | None = None
    parser: str | None = None
    chunk_size: int | float | str | None = None
    chunk_overlap: int | float | str | None = None
    chunk_index: int | None = None
    chunking_strategy: str | None = None
    document_id: str | None = None
    metadata: dict[str, Any] | None = None


class CitationDisplayResult(BaseModel):
    data: CitationDisplayData | None = None
    chunk_id: str | None = None
    id: str | None = None
    filename: str | None = None
    page: int | str | None = None
    score: float | str | None = None
    text: str | None = None
    embedding_model: str | None = None
    parser: str | None = None
    chunk_size: int | float | str | None = None
    chunk_overlap: int | float | str | None = None
    chunk_index: int | None = None
    chunking_strategy: str | None = None
    document_id: str | None = None
    source_url: str | None = None
    metadata: dict[str, Any] | None = None


class CitationDisplayResultGroup(BaseModel):
    results: list[CitationDisplayResult]


class OnboardingFunctionCall(BaseModel):
    name: str
    status: str
    result: list[CitationDisplayResultGroup | CitationDisplayResult] | None = None

    @field_validator("result", mode="before")
    @classmethod
    def ignore_non_list_result(cls, value: Any) -> Any:
        if value is None or isinstance(value, list):
            return value
        return None


class AssistantMessage(BaseModel):
    role: str
    content: str
    timestamp: str
    functionCalls: list[OnboardingFunctionCall] | None = None


class OnboardingStateBody(BaseModel):
    current_step: int | None = None
    assistant_message: AssistantMessage | None = None
    selected_nudge: str | None = None
    card_steps: dict[str, Any] | None = None
    upload_steps: dict[str, Any] | None = None
    openrag_docs_filter_id: str | None = None
    user_doc_filter_id: str | None = None
    openrag_docs_ingested_version: str | None = None
    openrag_docs_remote_signature: str | None = None


class DoclingPresetBody(BaseModel):
    preset: str | None = None
    table_structure: bool | None = None
    ocr: bool | None = None
    picture_descriptions: bool | None = None


class OnboardingStateConfig(BaseModel):
    current_step: int | None
    assistant_message: AssistantMessage | None
    selected_nudge: str | None
    card_steps: dict[str, Any] | None
    upload_steps: dict[str, Any] | None
    openrag_docs_filter_id: str | None
    user_doc_filter_id: str | None
    openrag_docs_ingested_version: str | None
    openrag_docs_remote_signature: str | None


class OpenAIProviderConfig(BaseModel):
    has_api_key: bool
    configured: bool


class AnthropicProviderConfig(BaseModel):
    has_api_key: bool
    configured: bool


class WatsonXProviderConfig(BaseModel):
    has_api_key: bool
    endpoint: str | None
    project_id: str | None
    configured: bool


class OllamaProviderConfig(BaseModel):
    endpoint: str | None
    configured: bool


class ProvidersConfig(BaseModel):
    openai: OpenAIProviderConfig
    anthropic: AnthropicProviderConfig
    watsonx: WatsonXProviderConfig
    ollama: OllamaProviderConfig


class KnowledgeConfig(BaseModel):
    embedding_model: str | None
    embedding_provider: str | None
    chunk_size: int | None
    chunk_overlap: int | None
    chunking_strategy: str | None = None
    hybrid_max_tokens: int | None = None
    hybrid_merge_peers: bool | None = None
    table_structure: bool | None
    ocr: bool | None
    picture_descriptions: bool | None
    index_name: str | None
    disable_ingest_with_langflow: bool | None
    retrieval_strategy: str | None = None
    retrieval_mode: str | None = None
    retrieval_lexical_candidates: int | None = None
    retrieval_vector_candidates: int | None = None
    retrieval_rrf_k: int | None = None
    retrieval_max_chunks_per_document: int | None = None
    retrieval_adaptive_max_chunks_per_document: int | None = None
    retrieval_reranker_url: str | None = None
    retrieval_reranker_timeout: int | None = None
    retrieval_debug: bool | None = None


class AgentConfig(BaseModel):
    llm_model: str | None
    llm_provider: str | None
    system_prompt: str | None


class ArchivingConfig(BaseModel):
    """Local source archiving state, paths, and storage statistics."""

    available: bool
    enabled: bool
    ingestion_path: str | None = None
    ingestion_host_path: str | None = None
    path: str | None = None
    host_path: str | None = None
    used_bytes: int | None = None
    filesystem_total_bytes: int | None = None
    filesystem_free_bytes: int | None = None


class IngestionDefaultsConfig(BaseModel):
    chunkSize: int | None
    chunkOverlap: int | None
    separator: str | None
    embeddingModel: str | None


class SettingsResponse(BaseModel):
    langflow_url: str
    flow_id: str | None
    ingest_flow_id: str | None
    langflow_public_url: str | None
    edited: bool
    onboarding: OnboardingStateConfig
    # None when the caller lacks `providers:read` (RBAC redaction for non-admins).
    providers: ProvidersConfig | None = None
    knowledge: KnowledgeConfig
    agent: AgentConfig
    # Filesystem paths and storage details are admin-only.
    archiving: ArchivingConfig | None = None
    localhost_url: str
    langflow_edit_url: str | None = None
    langflow_ingest_edit_url: str | None = None
    ingestion_defaults: IngestionDefaultsConfig | None = None
    ingest_via_chat: bool = False
    show_provider_ingest_settings: bool = False
    show_shared_upload_toggle: bool = False
    show_workspace_oauth_overrides: bool = False
    segment_write_key: str | None = None
    environment: str | None = None
    langflow_port: str | None = None


class OnboardingResponse(BaseModel):
    message: str
    edited: bool
    sample_data_ingested: bool
    openrag_docs_filter_id: str | None = None
    task_id: str | None = None


class RefreshOpenRAGDocsResponse(BaseModel):
    message: str
    refreshed: bool


class DoclingPresetResponse(BaseModel):
    message: str
    settings: dict
    preset_config: DoclingConfig


class OnboardingStateResponse(BaseModel):
    message: str
    updated_fields: list[str]


class SettingsUpdateResponse(BaseModel):
    message: str


class RollbackResponse(BaseModel):
    message: str
    cancelled_tasks: int
    deleted_files: int
    reset_flows: int
    deleted_conversations: int


class RollbackBody(BaseModel):
    embedding_only: bool = False
