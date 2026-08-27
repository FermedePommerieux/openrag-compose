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

export interface RankedDocumentChunk extends ChunkResult {
  relevance_rank?: number;
  relevance_score?: number;
}

export interface RankDocumentChunksOptions {
  chunks: ChunkResult[];
  filename: string;
  query: string;
  filters?: FilterInput;
  fetcher?: FetchLike;
}

/** Exhaustive reads are source-ordered and legitimately have no relevance score. */
export function formatDocumentChunkScore(score: unknown): string {
  return typeof score === "number" && Number.isFinite(score)
    ? `${score.toFixed(2)} score`
    : "Source order";
}

function chunkIdentity(chunk: ChunkResult): string {
  return (
    chunk.chunk_id?.trim() ||
    `${chunk.document_id || ""}:${chunk.chunk_index ?? chunk.page}:${chunk.text}`
  );
}

/**
 * Rank an already exhaustive chunk set without removing unscored evidence.
 *
 * Focused hybrid retrieval supplies meaningful relevance scores for the query.
 * Its candidates are merged back into the complete source-ordered set; chunks
 * outside the candidate window remain visible after the scored candidates.
 */
export async function rankDocumentChunks({
  chunks,
  filename,
  query,
  filters,
  fetcher = fetch,
}: RankDocumentChunksOptions): Promise<RankedDocumentChunk[]> {
  const normalizedQuery = query.trim();
  if (!normalizedQuery || normalizedQuery === "*" || chunks.length === 0) {
    return chunks;
  }

  const response = await fetcher("/api/search", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      query: normalizedQuery,
      // Exact source scoping makes this a ranking of the open document, not a
      // second corpus-wide retrieval. Existing ACL and filter dimensions stay.
      filters: { ...(filters ?? {}), data_sources: [filename] },
      limit: Math.max(50, chunks.length),
      scoreThreshold: 0,
      evidenceMode: "focused",
    }),
  });
  const payload = (await response.json()) as {
    results?: ChunkResult[];
    error?: string;
  };
  if (!response.ok) {
    throw new Error(
      payload.error || `Failed to rank document chunks: ${response.status}`,
    );
  }

  const rankedCandidates = Array.isArray(payload.results)
    ? payload.results
    : [];
  const scores = new Map<string, { rank: number; score: number }>();
  rankedCandidates.forEach((chunk, index) => {
    if (typeof chunk.score === "number" && Number.isFinite(chunk.score)) {
      scores.set(chunkIdentity(chunk), { rank: index + 1, score: chunk.score });
    }
  });

  return chunks
    .map((chunk): RankedDocumentChunk => {
      const ranking = scores.get(chunkIdentity(chunk));
      return ranking
        ? {
            ...chunk,
            relevance_rank: ranking.rank,
            relevance_score: ranking.score,
          }
        : { ...chunk };
    })
    .sort((left, right) => {
      const leftRank = left.relevance_rank ?? Number.POSITIVE_INFINITY;
      const rightRank = right.relevance_rank ?? Number.POSITIVE_INFINITY;
      if (leftRank !== rightRank) return leftRank - rightRank;
      return (
        (left.chunk_index ?? left.page) - (right.chunk_index ?? right.page)
      );
    });
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
