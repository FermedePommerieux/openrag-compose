import type { ToolCallResult } from "@/app/chat/_types/types";

const MAX_FALLBACK_SOURCE_CARDS = 10;

export interface CitedSource {
  item: ToolCallResult;
  index: number;
}

const addPrimaryLookupKey = (
  sourceLookup: Map<string, ToolCallResult>,
  key: string | undefined,
  source: ToolCallResult,
) => {
  if (key) sourceLookup.set(key, source);
};

const addFallbackLookupKey = (
  sourceLookup: Map<string, ToolCallResult | null>,
  key: string | undefined,
  source: ToolCallResult,
) => {
  if (!key) return;

  if (sourceLookup.has(key)) {
    sourceLookup.set(key, null);
    return;
  }

  sourceLookup.set(key, source);
};

/**
 * Derives a display filename from citation data.
 * Extracts the filename from a file path or uses the filename field directly.
 */
export const deriveDisplayFilename = (
  filePath: string | undefined,
  filename: string | undefined,
  fallback: string = "Document",
): string => {
  const path = filePath || filename || fallback;
  return path.split("/").pop() || path;
};

const buildSourceLookup = (sources: ToolCallResult[]) => {
  const primaryLookup = new Map<string, ToolCallResult>();
  const fallbackLookup = new Map<string, ToolCallResult | null>();

  for (const source of sources) {
    addPrimaryLookupKey(primaryLookup, source.chunk_id, source);
    addPrimaryLookupKey(primaryLookup, source.id, source);
    addFallbackLookupKey(fallbackLookup, source.data?.file_path, source);
    addFallbackLookupKey(fallbackLookup, source.filename, source);
  }

  return {
    get(id: string): ToolCallResult | undefined {
      const primarySource = primaryLookup.get(id);
      if (primarySource) return primarySource;

      const fallbackSource = fallbackLookup.get(id);
      return fallbackSource ?? undefined;
    },
  };
};

export const preprocessCitations = (
  text: string,
  sources: ToolCallResult[] | undefined,
): { text: string; citedSources: CitedSource[] } => {
  if (!sources || sources.length === 0) {
    return { text, citedSources: [] };
  }

  const sourceLookup = buildSourceLookup(sources);
  const citedSourcesMap = new Map<string, number>();
  const citedSourcesList: CitedSource[] = [];
  let nextIndex = 1;

  const addExactCitation = (rawId: string): string | undefined => {
    const trimmedId = rawId.trim();
    const exactId =
      trimmedId.length >= 2 &&
      trimmedId.startsWith("`") &&
      trimmedId.endsWith("`")
        ? trimmedId.slice(1, -1).trim()
        : trimmedId;
    const foundSource = sourceLookup.get(exactId);
    if (!foundSource) return undefined;

    const uniqueKey = (foundSource.chunk_id ||
      foundSource.id ||
      foundSource.filename ||
      JSON.stringify(foundSource)) as string;

    let index = citedSourcesMap.get(uniqueKey);
    if (index === undefined) {
      index = nextIndex++;
      citedSourcesMap.set(uniqueKey, index);
      citedSourcesList.push({ item: foundSource, index });
    }
    return `[\\[${index}\\]](#citation-${index})`;
  };

  // Patterns: (Source: chunk_id) or [Source: chunk_id]
  const regex = /\[Source:\s*([^\]]+)\]|\(Source:\s*([^)]+)\)/g;

  let processedText = text.replace(regex, (_match, p1, p2) => {
    const rawIds = p1 || p2;
    if (!rawIds) return "";

    const ids = rawIds.split(",").map((id: string) => id.trim());
    const replacementBadges: string[] = [];

    for (const rawId of ids) {
      const badge = addExactCitation(rawId);
      if (badge) replacementBadges.push(badge);
    }

    if (replacementBadges.length > 0) {
      return replacementBadges.join("");
    }

    return "";
  });

  // Models sometimes answer a direct "cite your sources" request with a list
  // of code-formatted chunk ids instead of the required Source wrapper. Only
  // promote a token when it exactly matches a structured retrieval artifact;
  // filenames, document ids, prose and invented ids remain ordinary code.
  processedText = processedText.replace(/`([^`\r\n]+)`/g, (match, rawId) => {
    return addExactCitation(rawId) ?? match;
  });

  // A model can use the retrieved evidence correctly while omitting the
  // required inline Source marker. Do not make the user expand the technical
  // function-call trace to reach provenance in that case: expose a bounded,
  // URL-deduplicated set of the structured retrieval sources below the answer.
  // Explicit citations remain authoritative whenever the model emitted them.
  if (citedSourcesList.length === 0) {
    const seenUrls = new Set<string>();
    for (const source of sources) {
      const sourceUrl = source.source_url?.trim();
      if (!sourceUrl || seenUrls.has(sourceUrl)) continue;
      seenUrls.add(sourceUrl);
      citedSourcesList.push({ item: source, index: nextIndex++ });
      if (citedSourcesList.length >= MAX_FALLBACK_SOURCE_CARDS) break;
    }
  }

  return { text: processedText, citedSources: citedSourcesList };
};

/**
 * Preserve the visible citation numbers when copying an assistant response,
 * without leaking internal chunk identifiers or Markdown-only anchor targets.
 */
export const prepareChatMessageForClipboard = (
  text: string,
  sources: ToolCallResult[] | undefined,
): string =>
  preprocessCitations(text, sources)
    .text.replace(/\[\\\[(\d+)\\\]\]\(#citation-\d+\)/g, "[$1]")
    .trim();
