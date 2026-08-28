/**
 * OpenRAG SDK types.
 */

// Source provenance follows OpenRAG's bounded W3C PROV-O profile. The role is
// the application meaning; prov_predicate is the corresponding full PROV URI.
export type SourceRelationRole =
  | "attachment_of"
  | "member_of"
  | "reply_to"
  | "references"
  | "contained_in"
  | "occurrence_of"
  | "derived_from"
  | "primary_source";

export interface SourceEntity {
  id: string;
  type: string;
  source_system?: string | null;
  label?: string | null;
  alternate_ids?: string[];
  generated_at_time?: string | null;
}

export interface SourceRelation {
  role: SourceRelationRole;
  target: SourceEntity;
  prov_predicate?: string | null;
}

export interface SourceProvenance {
  schema_version?: "1.0";
  entity: SourceEntity;
  relative_path?: string | null;
  relations?: SourceRelation[];
}

export interface SourceProvenanceFields {
  source_provenance?: SourceProvenance | null;
  source_entity_id?: string | null;
  source_entity_type?: string | null;
  source_entity_system?: string | null;
  source_entity_alternate_ids?: string[];
  source_relation_target_ids?: string[];
  source_relation_roles?: string[];
  source_relative_path?: string | null;
  source_path_ancestors?: string[];
}

// Chat types
export interface Source extends SourceProvenanceFields {
  filename: string;
  text: string;
  score: number | null;
  page?: number | null;
  mimetype?: string | null;
  source_url?: string | null;
  document_id?: string | null;
  chunk_id?: string | null;
  chunk_index?: number | null;
  chunking_strategy?: string | null;
  connector_file_id?: string | null;
  chunk_content_sha256?: string | null;
  document_content_sha256?: string | null;
  evidence_order?: number | null;
}

export interface ChatResponse {
  response: string;
  chatId?: string | null;
  sources: Source[];
}

export type StreamEventType = "content" | "sources" | "done";

export interface ContentEvent {
  type: "content";
  delta: string;
}

export interface SourcesEvent {
  type: "sources";
  sources: Source[];
}

export interface DoneEvent {
  type: "done";
  chatId?: string | null;
}

export type StreamEvent = ContentEvent | SourcesEvent | DoneEvent;

// Search types
export interface SearchResult extends SourceProvenanceFields {
  filename: string;
  text: string;
  score: number | null;
  page?: number | null;
  mimetype?: string | null;
  source_url?: string | null;
  document_id?: string | null;
  chunk_id?: string | null;
  chunk_index?: number | null;
  chunking_strategy?: string | null;
  connector_file_id?: string | null;
  chunk_content_sha256?: string | null;
  document_content_sha256?: string | null;
  evidence_order?: number | null;
}

export interface EvidenceCoverage {
  mode: "exhaustive";
  document_id: string;
  snapshot_sha256?: string | null;
  covered_chunks: number;
  total_chunks: number;
  coverage_ratio?: number | null;
  complete: boolean;
  next_cursor?: string | null;
}

export interface SearchResponse {
  results: SearchResult[];
  coverage?: EvidenceCoverage;
  error?: string;
}

export interface SearchFilters {
  data_sources?: string[];
  document_types?: string[];
}

// Document types
export interface IngestResponse {
  task_id: string;
  status?: string | null;  // Optional - we poll for actual status
  filename?: string | null;
}

export interface IngestTaskStatus {
  task_id: string;
  status: string; // "pending", "running", "completed", "failed"
  total_files: number;
  processed_files: number;
  successful_files: number;
  failed_files: number;
  files: Record<string, unknown>;
}

export interface DeleteDocumentResponse {
  success: boolean;
  deleted_chunks: number;
  filename?: string | null;
  message?: string | null;
  error?: string | null;
  // Populated when deleting by filter_id — one entry per resolved data_source.
  filenames?: string[] | null;
  filter_id?: string | null;
  per_file?: Array<Record<string, unknown>> | null;
}

export interface DeleteDocumentOptions {
  filename?: string;
  filterId?: string;
}

// Chat history types
export interface Message {
  role: string;
  content: string;
  timestamp?: string | null;
}

export interface Conversation {
  chatId: string;
  title: string;
  createdAt?: string | null;
  lastActivity?: string | null;
  messageCount: number;
}

export interface ConversationDetail extends Conversation {
  messages: Message[];
}

export interface ConversationListResponse {
  conversations: Conversation[];
}

// Settings types
export interface AgentSettings {
  llm_provider?: string | null;
  llm_model?: string | null;
}

export interface KnowledgeSettings {
  embedding_provider?: string | null;
  embedding_model?: string | null;
  chunk_size?: number | null;
  chunk_overlap?: number | null;
  table_structure?: boolean | null;
  ocr?: boolean | null;
  ocr_mojibake_fallback?: boolean | null;
  picture_descriptions?: boolean | null;
  chunking_strategy?: "character" | "hybrid" | null;
  hybrid_max_tokens?: number | null;
  hybrid_merge_peers?: boolean | null;
  retrieval_strategy?: "weighted" | "rrf" | null;
  retrieval_mode?: "hybrid" | "lexical" | "vector" | null;
  retrieval_lexical_candidates?: number | null;
  retrieval_vector_candidates?: number | null;
  retrieval_rrf_k?: number | null;
  retrieval_max_chunks_per_document?: number | null;
  retrieval_adaptive_max_chunks_per_document?: number | null;
  retrieval_reranker_url?: string | null;
  retrieval_reranker_timeout?: number | null;
  retrieval_debug?: boolean | null;
}

export interface ArchivingSettings {
  available: boolean;
  enabled: boolean;
}

export interface SettingsResponse {
  agent: AgentSettings;
  knowledge: KnowledgeSettings;
  archiving: ArchivingSettings;
}

/** Options for updating settings. */
export interface SettingsUpdateOptions {
  /** LLM model name. */
  llm_model?: string;
  /** LLM provider (openai, anthropic, watsonx, ollama). */
  llm_provider?: string;
  /** System prompt for the agent. */
  system_prompt?: string;
  /** Embedding model name. */
  embedding_model?: string;
  /** Embedding provider (openai, watsonx, ollama). */
  embedding_provider?: string;
  /** Chunk size for document splitting. */
  chunk_size?: number;
  /** Chunk overlap for document splitting. */
  chunk_overlap?: number;
  /** Enable table structure parsing. */
  table_structure?: boolean;
  /** Enable OCR for text extraction. */
  ocr?: boolean;
  /** Retry PDFs with a broken character map using full-page OCR. */
  ocr_mojibake_fallback?: boolean;
  /** Enable picture descriptions. */
  picture_descriptions?: boolean;
  /** Keep original local source files after successful ingestion. */
  archive_sources_enabled?: boolean;
  /** Character splitter or optional structure-aware Docling HybridChunker. */
  chunking_strategy?: "character" | "hybrid";
  hybrid_max_tokens?: number;
  hybrid_merge_peers?: boolean;
  /** Historical weighted query or deterministic reciprocal-rank fusion. */
  retrieval_strategy?: "weighted" | "rrf";
  retrieval_mode?: "hybrid" | "lexical" | "vector";
  retrieval_lexical_candidates?: number;
  retrieval_vector_candidates?: number;
  retrieval_rrf_k?: number;
  retrieval_max_chunks_per_document?: number;
  retrieval_adaptive_max_chunks_per_document?: number;
  /** Administrator-configured HTTP reranker endpoint. */
  retrieval_reranker_url?: string;
  retrieval_reranker_timeout?: number;
  retrieval_debug?: boolean;
}

/** Response from settings update. */
export interface SettingsUpdateResponse {
  message: string;
}

// Knowledge filter types
/** Query configuration stored in a knowledge filter. */
export interface KnowledgeFilterQueryData {
  /** Semantic search query text. */
  query?: string;
  /** Filter criteria for documents. */
  filters?: {
    data_sources?: string[];
    document_types?: string[];
    owners?: string[];
    connector_types?: string[];
  };
  /** Maximum number of results. */
  limit?: number;
  /** Minimum relevance score threshold. */
  scoreThreshold?: number;
  /** UI color for the filter. */
  color?: string;
  /** UI icon for the filter. */
  icon?: string;
}

/** A knowledge filter definition. */
export interface KnowledgeFilter {
  id: string;
  name: string;
  description?: string;
  queryData: KnowledgeFilterQueryData;
  owner?: string;
  createdAt?: string;
  updatedAt?: string;
}

/** Options for creating a knowledge filter. */
export interface CreateKnowledgeFilterOptions {
  /** Filter name (required). */
  name: string;
  /** Filter description. */
  description?: string;
  /** Query configuration for the filter. */
  queryData: KnowledgeFilterQueryData;
}

/** Options for updating a knowledge filter. */
export interface UpdateKnowledgeFilterOptions {
  /** New filter name. */
  name?: string;
  /** New filter description. */
  description?: string;
  /** New query configuration. */
  queryData?: KnowledgeFilterQueryData;
}

/** Response from creating a knowledge filter. */
export interface CreateKnowledgeFilterResponse {
  success: boolean;
  id?: string;
  error?: string;
}

/** Response from searching knowledge filters. */
export interface KnowledgeFilterSearchResponse {
  success: boolean;
  filters: KnowledgeFilter[];
}

/** Response from getting a knowledge filter. */
export interface GetKnowledgeFilterResponse {
  success: boolean;
  filter?: KnowledgeFilter;
  error?: string;
}

/** Response from deleting a knowledge filter. */
export interface DeleteKnowledgeFilterResponse {
  success: boolean;
  error?: string;
}

// Client options
export interface OpenRAGClientOptions {
  /** API key for authentication. Falls back to OPENRAG_API_KEY env var.
   *  Optional when using IBM auth — pass credentials via extraHeaders instead. */
  apiKey?: string;
  /** Additional headers forwarded on every request. Used in IBM auth mode
   *  to pass X-Username and X-Api-Key from the user's MCP config. */
  extraHeaders?: Record<string, string>;
  /** Base URL for the API. Falls back to OPENRAG_URL env var. */
  baseUrl?: string;
  /** Request timeout in milliseconds (default 30000). */
  timeout?: number;
}

// Request types
export interface ChatCreateOptions {
  message: string;
  stream?: boolean;
  chatId?: string;
  filters?: SearchFilters;
  limit?: number;
  scoreThreshold?: number;
  /** Knowledge filter ID to apply to the chat. */
  filterId?: string;
}

export interface SearchQueryOptions {
  query: string;
  filters?: SearchFilters;
  limit?: number;
  scoreThreshold?: number;
  /** Knowledge filter ID to apply to the search. */
  filterId?: string;
  /** Ranked discovery or complete source-order evidence reading. */
  evidenceMode?: "focused" | "exhaustive";
  /** Required for exhaustive mode. Obtain it from focused discovery. */
  documentId?: string;
  /** Opaque continuation cursor returned in coverage.next_cursor. */
  cursor?: string;
  /** Exhaustive page size, from 1 to 50. */
  batchSize?: number;
}

// Error types
export class OpenRAGError extends Error {
  constructor(
    message: string,
    public statusCode?: number
  ) {
    super(message);
    this.name = "OpenRAGError";
  }
}

export class AuthenticationError extends OpenRAGError {
  constructor(message: string, statusCode?: number) {
    super(message, statusCode);
    this.name = "AuthenticationError";
  }
}

export class NotFoundError extends OpenRAGError {
  constructor(message: string, statusCode?: number) {
    super(message, statusCode);
    this.name = "NotFoundError";
  }
}

export class ValidationError extends OpenRAGError {
  constructor(message: string, statusCode?: number) {
    super(message, statusCode);
    this.name = "ValidationError";
  }
}

export class RateLimitError extends OpenRAGError {
  constructor(message: string, statusCode?: number) {
    super(message, statusCode);
    this.name = "RateLimitError";
  }
}

export class ServerError extends OpenRAGError {
  constructor(message: string, statusCode?: number) {
    super(message, statusCode);
    this.name = "ServerError";
  }
}
