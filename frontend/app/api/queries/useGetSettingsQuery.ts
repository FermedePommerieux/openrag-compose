import {
  type UseQueryOptions,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import type { FunctionCall } from "@/app/chat/_types/types";

export interface AgentSettings {
  llm_model?: string;
  llm_provider?: string;
  system_prompt?: string;
}

export interface KnowledgeSettings {
  embedding_model?: string;
  embedding_provider?: string;
  chunk_size?: number;
  chunk_overlap?: number;
  chunking_strategy?: "character" | "hybrid";
  hybrid_max_tokens?: number;
  hybrid_merge_peers?: boolean;
  table_structure?: boolean;
  ocr?: boolean;
  picture_descriptions?: boolean;
  picture_description_vlm_configured?: boolean;
  disable_ingest_with_langflow?: boolean;
  retrieval_strategy?: "weighted" | "rrf";
  retrieval_mode?: "hybrid" | "lexical" | "vector";
  retrieval_lexical_candidates?: number;
  retrieval_vector_candidates?: number;
  retrieval_rrf_k?: number;
  retrieval_max_chunks_per_document?: number;
  retrieval_adaptive_max_chunks_per_document?: number;
  retrieval_reranker_url?: string;
  retrieval_reranker_timeout?: number;
  retrieval_debug?: boolean;
}

export interface ProviderSettings {
  openai?: {
    has_api_key?: boolean;
    configured?: boolean;
  };
  anthropic?: {
    has_api_key?: boolean;
    configured?: boolean;
  };
  watsonx?: {
    has_api_key?: boolean;
    endpoint?: string;
    project_id?: string;
    configured?: boolean;
  };
  ollama?: {
    endpoint?: string;
    configured?: boolean;
  };
}

export interface OnboardingState {
  current_step?: number;
  assistant_message?: {
    role: string;
    content: string;
    timestamp: string;
    functionCalls?: FunctionCall[] | null;
  } | null;
  selected_nudge?: string | null;
  card_steps?: Record<string, unknown> | null;
  upload_steps?: Record<string, unknown> | null;
  openrag_docs_filter_id?: string | null;
  user_doc_filter_id?: string | null;
}

export interface Settings {
  langflow_url?: string;
  flow_id?: string;
  ingest_flow_id?: string;
  langflow_public_url?: string;
  edited?: boolean;
  onboarding?: OnboardingState;
  providers?: ProviderSettings;
  knowledge?: KnowledgeSettings;
  agent?: AgentSettings;
  archiving?: {
    available: boolean;
    enabled: boolean;
    ingestion_path?: string | null;
    ingestion_host_path?: string | null;
    path?: string | null;
    host_path?: string | null;
    used_bytes?: number | null;
    filesystem_total_bytes?: number | null;
    filesystem_free_bytes?: number | null;
  };
  langflow_edit_url?: string;
  langflow_ingest_edit_url?: string;
  ingestion_defaults?: {
    chunkSize?: number;
    chunkOverlap?: number;
    separator?: string;
    embeddingModel?: string;
  };
  localhost_url?: string;
  ingest_via_chat?: boolean;
  show_provider_ingest_settings?: boolean;
  show_shared_upload_toggle?: boolean;
  show_workspace_oauth_overrides?: boolean;
  segment_write_key?: string;
  environment?: string;
  langflow_port?: string | number | null;
}

async function getSettings(includeArchivingStats = false): Promise<Settings> {
  const query = includeArchivingStats ? "?include_archiving_stats=true" : "";
  const response = await fetch(`/api/settings${query}`);
  if (response.ok) {
    return await response.json();
  } else {
    throw new Error("Failed to fetch settings");
  }
}

export const useGetSettingsQuery = (
  options?: Omit<UseQueryOptions<Settings>, "queryKey" | "queryFn">,
) => {
  const queryClient = useQueryClient();

  return useQuery(
    {
      queryKey: ["settings"],
      queryFn: () => getSettings(false),
      ...options,
    },
    queryClient,
  );
};

export const useGetArchivingSettingsQuery = (
  options?: Omit<UseQueryOptions<Settings>, "queryKey" | "queryFn">,
) => {
  const queryClient = useQueryClient();

  return useQuery(
    {
      queryKey: ["settings", "archiving"],
      queryFn: () => getSettings(true),
      ...options,
    },
    queryClient,
  );
};
