export interface TokenUsage {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  input_tokens_details?: {
    cached_tokens?: number;
  };
  output_tokens_details?: {
    reasoning_tokens?: number;
  };
}

export interface Message {
  role: "user" | "assistant";
  content: string;
  timestamp: Date;
  functionCalls?: FunctionCall[];
  isStreaming?: boolean;
  source?: "langflow" | "chat";
  error?: boolean;
  usage?: TokenUsage;
}

export const INITIAL_ASSISTANT_MESSAGE: Message = {
  role: "assistant",
  content: "How can I assist?",
  timestamp: new Date(),
};

export interface FunctionCall {
  name: string;
  arguments?: Record<string, unknown>;
  result?: Record<string, unknown> | ToolCallResult[];
  status: "pending" | "completed" | "error";
  argumentsString?: string;
  id?: string;
  type?: string;
}

export interface ToolCallResult {
  text_key?: string;
  data?: {
    file_path?: string;
    text?: string;
    page?: number | string;
    score?: number | string;
    embedding_model?: string;
    parser?: string;
    chunk_size?: number | string;
    chunk_overlap?: number | string;
    mimetype?: string;
    document_id?: string;
    connector_file_id?: string;
    chunk_index?: number | string;
    chunking_strategy?: string;
    metadata?: {
      embedding_model?: string;
      parser?: string;
      page?: number | string;
      score?: number | string;
      chunk_size?: number | string;
      chunk_overlap?: number | string;
      document_id?: string;
      connector_file_id?: string;
      chunk_index?: number | string;
      chunking_strategy?: string;
      [key: string]: unknown;
    };
    [key: string]: unknown;
  };
  default_value?: string;
  chunk_id?: string;
  id?: string;
  filename?: string;
  page?: number | string;
  score?: number | string;
  source_url?: string | null;
  source_provenance?: Record<string, unknown> | null;
  source_entity_id?: string;
  source_entity_type?: string;
  source_entity_system?: string;
  source_entity_alternate_ids?: string[];
  source_relation_target_ids?: string[];
  source_relation_roles?: string[];
  source_relative_path?: string;
  source_path_ancestors?: string[];
  text?: string;
  embedding_model?: string;
  parser?: string;
  chunk_size?: number | string;
  chunk_overlap?: number | string;
  mimetype?: string;
  document_id?: string;
  connector_file_id?: string;
  chunk_index?: number | string;
  chunking_strategy?: string;
  metadata?: {
    embedding_model?: string;
    parser?: string;
    page?: number | string;
    score?: number | string;
    chunk_size?: number | string;
    chunk_overlap?: number | string;
    document_id?: string;
    connector_file_id?: string;
    chunk_index?: number | string;
    chunking_strategy?: string;
    [key: string]: unknown;
  };
  [key: string]: unknown;
}

export interface SelectedFilters {
  data_sources: string[];
  document_types: string[];
  owners: string[];
  connector_types: string[];
}

export interface KnowledgeFilterData {
  id: string;
  name: string;
  description: string;
  query_data: string;
  owner: string;
  created_at: string;
  updated_at: string;
}

export interface RequestBody {
  prompt: string;
  stream?: boolean;
  previous_response_id?: string;
  filters?: {
    data_sources?: string[];
    document_types?: string[];
    owners?: string[];
    connector_types?: string[];
  };
  filter_id?: string;
  limit?: number;
  scoreThreshold?: number;
}
