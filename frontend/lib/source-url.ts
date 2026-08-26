/** Return a safe downloadable source URL when the source is supported. */
export function getDownloadSourceUrl(sourceUrl?: string): string | undefined {
  const url = sourceUrl?.trim();
  if (!url) return undefined;

  try {
    const parsed = new URL(url, "http://openrag.local");
    if (!["http:", "https:"].includes(parsed.protocol)) return undefined;
    if (parsed.username || parsed.password) return undefined;

    const isManagedLocalSource =
      url.startsWith("/api/source-files/") &&
      parsed.pathname.startsWith("/api/source-files/");
    const isAbsoluteHttpUrl = /^https?:\/\//i.test(url);
    if (!isAbsoluteHttpUrl && !isManagedLocalSource) return undefined;
    return url;
  } catch {
    return undefined;
  }
}

/** Return whether a source URL resolves to a backend-managed archived file. */
export function isArchivedSourceUrl(sourceUrl?: string): boolean {
  const url = getDownloadSourceUrl(sourceUrl);
  if (!url) return false;

  const parsed = new URL(url, "http://openrag.local");
  const marker = "/api/source-files/";
  const markerIndex = parsed.pathname.lastIndexOf(marker);
  if (markerIndex < 0) return false;

  const sourceId = parsed.pathname.slice(markerIndex + marker.length);
  return /^[A-Za-z0-9_-]{16,128}\.[a-f0-9]{32}$/.test(sourceId);
}

export type SourcePreviewKind = "image" | "document";

/**
 * Return the iframe sandbox policy for a document preview.
 *
 * Chromium's built-in PDF viewer cannot render inside an iframe restricted to
 * `allow-downloads`; it produces an empty white frame even though the PDF was
 * fetched successfully. PDFs are therefore allowed to use the native viewer
 * without a sandbox. Other document types keep the restrictive policy because
 * they may contain browser-renderable markup.
 */
export function getDocumentPreviewSandbox(
  filename: string,
  mimetype?: string,
): "allow-downloads" | undefined {
  const normalizedMimeType = mimetype?.split(";", 1)[0].trim().toLowerCase();
  const isPdf =
    normalizedMimeType === "application/pdf" ||
    filename.toLowerCase().endsWith(".pdf");
  return isPdf ? undefined : "allow-downloads";
}

const IMAGE_MIME_TYPES = new Set([
  "image/avif",
  "image/bmp",
  "image/gif",
  "image/jpeg",
  "image/png",
  "image/webp",
]);

const DOCUMENT_MIME_TYPES = new Set([
  "application/json",
  "application/pdf",
  "text/csv",
  "text/markdown",
  "text/plain",
]);

/** Return the native preview renderer supported for a source file. */
export function getSourcePreviewKind(
  filename: string,
  mimetype?: string,
): SourcePreviewKind | undefined {
  const normalizedMimeType = mimetype?.split(";", 1)[0].trim().toLowerCase();
  if (normalizedMimeType && IMAGE_MIME_TYPES.has(normalizedMimeType)) {
    return "image";
  }
  if (normalizedMimeType && DOCUMENT_MIME_TYPES.has(normalizedMimeType)) {
    return "document";
  }

  const extension = filename.split(".").pop()?.toLowerCase();
  if (
    ["avif", "bmp", "gif", "jpeg", "jpg", "png", "webp"].includes(
      extension ?? "",
    )
  ) {
    return "image";
  }
  if (["csv", "json", "md", "pdf", "txt"].includes(extension ?? "")) {
    return "document";
  }
  return undefined;
}

/** Return an inline URL for a safe source preview, optionally targeting a PDF page. */
export function getPreviewSourceUrl(
  sourceUrl?: string,
  referencePage?: number,
): string | undefined {
  const url = getDownloadSourceUrl(sourceUrl);
  if (!url) return undefined;

  let previewUrl = url;
  if (url.startsWith("/api/source-files/")) {
    const parsed = new URL(url, "http://openrag.local");
    parsed.searchParams.set("preview", "true");
    previewUrl = `${parsed.pathname}${parsed.search}${parsed.hash}`;
  }

  if (referencePage && Number.isFinite(referencePage) && referencePage > 0) {
    return `${previewUrl.split("#", 1)[0]}#page=${Math.floor(referencePage)}`;
  }
  return previewUrl;
}
