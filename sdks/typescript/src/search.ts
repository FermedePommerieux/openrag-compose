/**
 * OpenRAG SDK search client.
 */

import type { OpenRAGClient } from "./client";
import type { SearchQueryOptions, SearchResponse } from "./types";

export class SearchClient {
  constructor(private client: OpenRAGClient) {}

  /**
   * Find ranked matches or read one immutable document snapshot exhaustively.
   *
   * @param query - The search query text.
   * @param options - Optional search options.
   * @returns Focused matches, or an exhaustive page with a coverage certificate.
   * Continue with coverage.next_cursor until coverage.complete is true.
   */
  async query(
    query: string,
    options?: Omit<SearchQueryOptions, "query">
  ): Promise<SearchResponse> {
    const body: Record<string, unknown> = {
      query,
      limit: options?.limit ?? 10,
      score_threshold: options?.scoreThreshold ?? 0,
      evidence_mode: options?.evidenceMode ?? "focused",
    };

    if (options?.filters) {
      body["filters"] = options.filters;
    }

    if (options?.filterId) {
      body["filter_id"] = options.filterId;
    }
    if (options?.documentId) body["document_id"] = options.documentId;
    if (options?.cursor) body["cursor"] = options.cursor;
    if (options?.batchSize) body["batch_size"] = options.batchSize;

    const response = await this.client._request("POST", "/api/v1/search", {
      body: JSON.stringify(body),
    });

    const data = await response.json();
    return {
      results: data.results || [],
      coverage: data.coverage,
      error: data.error,
      documents: data.documents || [],
      evidence_batches: data.evidence_batches || [],
      graph: data.graph,
    };
  }
}
