import type { ChunkResult } from "@/app/api/queries/useGetSearchQuery";
import type { FilterInput } from "@/lib/filter-normalization";

export interface ExhaustiveCoverage {
  mode?: string;
  document_id?: string;
  complete?: boolean;
  next_cursor?: string;
  covered_chunks?: number;
  total_chunks?: number;
}

interface ExhaustiveSearchResponse {
  results?: ChunkResult[];
  coverage?: ExhaustiveCoverage;
  error?: string;
}

type FetchLike = (
  input: string,
  init?: RequestInit,
) => Promise<Pick<Response, "ok" | "status" | "json">>;

export interface FetchDocumentChunksOptions {
  documentId: string;
  query?: string;
  filters?: FilterInput;
  batchSize?: number;
  fetcher?: FetchLike;
}

/**
 * Read one immutable document snapshot to completion.
 *
 * The knowledge detail page used to run a corpus-wide `*` search and then look
 * for its filename in a bounded result set. Large corpora therefore displayed
 * "No knowledge" for valid documents. The exhaustive API is the authoritative
 * document reader: every cursor is followed and an incomplete or looping
 * coverage certificate is treated as an error, never as an empty document.
 */
export async function fetchAllDocumentChunks({
  documentId,
  query = "*",
  filters,
  batchSize = 50,
  fetcher = fetch,
}: FetchDocumentChunksOptions): Promise<ChunkResult[]> {
  const normalizedDocumentId = documentId.trim();
  if (!normalizedDocumentId) {
    throw new Error("A document_id is required to load document chunks");
  }

  const chunks: ChunkResult[] = [];
  const seenCursors = new Set<string>();
  let cursor = "";

  for (;;) {
    const response = await fetcher("/api/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        query: query.trim() || "*",
        filters: filters ?? {},
        limit: batchSize,
        scoreThreshold: 0,
        evidenceMode: "exhaustive",
        documentId: normalizedDocumentId,
        cursor,
        batchSize,
      }),
    });
    const payload = (await response.json()) as ExhaustiveSearchResponse;
    if (!response.ok) {
      throw new Error(
        payload.error || `Failed to load document chunks: ${response.status}`,
      );
    }

    if (!Array.isArray(payload.results) || !payload.coverage) {
      throw new Error("The exhaustive response has no coverage certificate");
    }
    if (
      payload.coverage.document_id &&
      payload.coverage.document_id !== normalizedDocumentId
    ) {
      throw new Error("The exhaustive response belongs to another document");
    }

    chunks.push(...payload.results);
    if (payload.coverage.complete === true) {
      return chunks;
    }

    const nextCursor = payload.coverage.next_cursor?.trim() || "";
    if (!nextCursor) {
      throw new Error("Document coverage is incomplete and has no next cursor");
    }
    if (seenCursors.has(nextCursor)) {
      throw new Error("Document coverage returned a repeated cursor");
    }
    seenCursors.add(nextCursor);
    cursor = nextCursor;
  }
}
