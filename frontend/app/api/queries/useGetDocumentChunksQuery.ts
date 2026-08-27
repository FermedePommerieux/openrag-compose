import { useQuery } from "@tanstack/react-query";
import type { ParsedQueryData } from "@/contexts/knowledge-filter-context";
import {
  fetchAllDocumentChunks,
  type RankedDocumentChunk,
  rankDocumentChunks,
} from "@/lib/document-chunks";
import { buildSearchPayloadFilters } from "@/lib/filter-normalization";
import type { ChunkResult, File } from "./useGetSearchQuery";

interface DocumentChunksResult {
  chunks: ChunkResult[];
  documentId: string;
  file?: File;
  rankedChunks: RankedDocumentChunk[];
  relevanceError?: string;
  relevanceQuery?: string;
}

async function resolveDocumentId(filename: string): Promise<string> {
  const searchParams = new URLSearchParams({ page: "1", page_size: "1" });
  searchParams.append("data_sources", filename);
  const response = await fetch(`/api/files?${searchParams.toString()}`);
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(
      payload.error ||
        `Failed to resolve document identity: ${response.status}`,
    );
  }

  const exactFile = Array.isArray(payload.files) ? payload.files[0] : undefined;
  const documentId = String(exactFile?.document_id || "").trim();
  if (!documentId || exactFile?.filename !== filename) {
    throw new Error("The requested document is not available in this context");
  }
  return documentId;
}

function fileFromChunks(chunks: ChunkResult[]): File | undefined {
  const chunk = chunks[0];
  if (!chunk) return undefined;
  return {
    document_id: chunk.document_id,
    filename: chunk.filename,
    mimetype: chunk.mimetype,
    chunkCount: chunks.length,
    avgScore:
      chunks.reduce((total, item) => total + (item.score || 0), 0) /
      chunks.length,
    source_url: chunk.source_url || "",
    source_provenance: chunk.source_provenance,
    source_entity_id: chunk.source_entity_id,
    source_entity_type: chunk.source_entity_type,
    source_entity_system: chunk.source_entity_system,
    source_entity_alternate_ids: chunk.source_entity_alternate_ids || [],
    source_relation_target_ids: chunk.source_relation_target_ids || [],
    source_relation_roles: chunk.source_relation_roles || [],
    owner: chunk.owner || "",
    owner_name: chunk.owner_name || "",
    owner_email: chunk.owner_email || "",
    size: chunk.file_size || 0,
    connector_type: chunk.connector_type || "local",
    embedding_model: chunk.embedding_model,
    embedding_dimensions: chunk.embedding_dimensions,
    allowed_users: chunk.allowed_users || [],
    allowed_groups: chunk.allowed_groups || [],
    chunks,
    status: "active",
  };
}

/** Load an exact document, preserving ACL/filter scope and full coverage. */
export const useGetDocumentChunksQuery = (
  filename: string | null,
  documentId: string | null,
  query: string,
  queryData?: ParsedQueryData | null,
) =>
  useQuery({
    queryKey: [
      "documentChunks",
      filename,
      documentId,
      query,
      queryData?.filters,
    ],
    enabled: Boolean(filename),
    retry: false,
    queryFn: async (): Promise<DocumentChunksResult> => {
      if (!filename) {
        throw new Error("No file specified");
      }
      const resolvedDocumentId =
        documentId?.trim() || (await resolveDocumentId(filename));
      const chunks = await fetchAllDocumentChunks({
        documentId: resolvedDocumentId,
        query: query || queryData?.query || "*",
        filters: buildSearchPayloadFilters(queryData?.filters),
      });
      const sourceOrderedChunks = chunks.map((chunk, index) => ({
        ...chunk,
        index: index + 1,
      }));
      const relevanceQuery = (query || queryData?.query || "").trim();
      let rankedChunks: RankedDocumentChunk[] = sourceOrderedChunks;
      let relevanceError: string | undefined;
      if (relevanceQuery && relevanceQuery !== "*") {
        try {
          rankedChunks = await rankDocumentChunks({
            chunks: sourceOrderedChunks,
            filename,
            query: relevanceQuery,
            filters: buildSearchPayloadFilters(queryData?.filters),
          });
        } catch (error) {
          // Relevance is an optional view. A ranking failure must never hide
          // the successfully certified exhaustive source-order evidence.
          relevanceError =
            error instanceof Error ? error.message : "Ranking failed";
        }
      }
      return {
        chunks: sourceOrderedChunks,
        documentId: resolvedDocumentId,
        file: fileFromChunks(sourceOrderedChunks),
        rankedChunks,
        relevanceError,
        relevanceQuery:
          relevanceQuery && relevanceQuery !== "*" ? relevanceQuery : undefined,
      };
    },
  });
