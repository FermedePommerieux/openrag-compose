import {
  type UseQueryOptions,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import type { File } from "./useGetSearchQuery";

export interface ListFilesParams {
  page?: number;
  pageSize?: number;
  sortBy?: string;
  sortOrder?: "asc" | "desc";
  connectorType?: string;
  mimetype?: string;
  owner?: string;
  search?: string;
  cursor?: string;
}

export interface ListFilesResponse {
  files: File[];
  total: number;
  page: number;
  page_size: number;
  next_cursor: string | null;
  prefetched_pages: PrefetchedListFilesPage[];
}

export interface PrefetchedListFilesPage {
  page: number;
  cursor: string;
  files: File[];
  next_cursor: string | null;
}

const mapFiles = (rawFiles: Record<string, unknown>[]): File[] =>
  rawFiles.map((f) => ({
    document_id: (f.document_id as string) || undefined,
    filename: (f.filename as string) || "",
    mimetype: (f.mimetype as string) || "",
    chunkCount: (f.chunk_count as number) || 0,
    source_url: (f.source_url as string) || "",
    source_provenance: f.source_provenance as
      | Record<string, unknown>
      | undefined,
    source_entity_id: f.source_entity_id as string | undefined,
    source_entity_type: f.source_entity_type as string | undefined,
    source_entity_system: f.source_entity_system as string | undefined,
    source_entity_alternate_ids:
      (f.source_entity_alternate_ids as string[]) || [],
    source_relation_target_ids:
      (f.source_relation_target_ids as string[]) || [],
    source_relation_roles: (f.source_relation_roles as string[]) || [],
    source_relative_path: f.source_relative_path as string | undefined,
    source_path_ancestors: (f.source_path_ancestors as string[]) || [],
    owner: (f.owner as string) || "",
    owner_name: (f.owner_name as string) || "",
    owner_email: (f.owner_email as string) || "",
    size: (f.file_size as number) || 0,
    connector_type: (f.connector_type as string) || "local",
    embedding_model: f.embedding_model as string | undefined,
    embedding_dimensions: f.embedding_dimensions as number | undefined,
    allowed_users: (f.allowed_users as string[]) || [],
    allowed_groups: (f.allowed_groups as string[]) || [],
    status: "active" as const,
  }));

export const useListFiles = (
  params: ListFilesParams = {},
  options?: Omit<UseQueryOptions<ListFilesResponse>, "queryKey" | "queryFn">,
) => {
  const queryClient = useQueryClient();

  async function fetchFiles(): Promise<ListFilesResponse> {
    const searchParams = new URLSearchParams();

    if (params.page) searchParams.set("page", String(params.page));
    if (params.pageSize) searchParams.set("page_size", String(params.pageSize));
    if (params.sortBy) searchParams.set("sort_by", params.sortBy);
    if (params.sortOrder) searchParams.set("sort_order", params.sortOrder);
    if (params.connectorType)
      searchParams.set("connector_type", params.connectorType);
    if (params.mimetype) searchParams.set("mimetype", params.mimetype);
    if (params.owner) searchParams.set("owner", params.owner);
    if (params.search) searchParams.set("search", params.search);
    if (params.cursor) searchParams.set("cursor", params.cursor);

    const url = `/api/files?${searchParams.toString()}`;
    const response = await fetch(url);

    if (!response.ok) {
      const errorData = await response
        .json()
        .catch(() => ({ error: "Unknown error" }));
      throw new Error(
        errorData.error || `Failed to list files: ${response.status}`,
      );
    }

    const data = await response.json();

    const files = mapFiles(data.files || []);
    const prefetchedPages: PrefetchedListFilesPage[] = (
      data.prefetched_pages || []
    ).map((prefetchedPage: Record<string, unknown>) => ({
      page: prefetchedPage.page as number,
      cursor: prefetchedPage.cursor as string,
      files: mapFiles(
        (prefetchedPage.files as Record<string, unknown>[]) || [],
      ),
      next_cursor: (prefetchedPage.next_cursor as string) || null,
    }));

    const result: ListFilesResponse = {
      files,
      total: data.total || 0,
      page: data.page || 1,
      page_size: data.page_size || 25,
      next_cursor: data.next_cursor || null,
      prefetched_pages: prefetchedPages,
    };

    // Seed React Query with the five bounded look-ahead pages returned by the
    // backend. Navigating forward reuses them immediately instead of querying
    // OpenSearch again on every click.
    for (const prefetchedPage of prefetchedPages) {
      const prefetchedParams: ListFilesParams = {
        ...params,
        page: prefetchedPage.page,
        cursor: prefetchedPage.cursor,
      };
      queryClient.setQueryData<ListFilesResponse>(
        ["listFiles", prefetchedParams],
        {
          files: prefetchedPage.files,
          total: data.total || 0,
          page: prefetchedPage.page,
          page_size: data.page_size || 25,
          next_cursor: prefetchedPage.next_cursor,
          prefetched_pages: [],
        },
      );
    }

    return result;
  }

  return useQuery(
    {
      queryKey: ["listFiles", params],
      placeholderData: (prev: ListFilesResponse | undefined) => prev,
      queryFn: fetchFiles,
      retry: false,
      // The response already contains five look-ahead pages. Keep that bounded
      // block fresh long enough to avoid repeatedly transferring up to 6,000
      // rows when the user explicitly selects the 1,000-row view.
      staleTime: 30_000,
      ...options,
    },
    queryClient,
  );
};
