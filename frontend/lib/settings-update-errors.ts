type ValidationDetail = {
  loc?: unknown;
  msg?: unknown;
};

function firstString(...values: unknown[]): string | undefined {
  return values.find(
    (value): value is string =>
      typeof value === "string" && value.trim().length > 0,
  );
}

function formatValidationDetail(detail: unknown[]): string | undefined {
  const first = detail.find(
    (item): item is ValidationDetail =>
      typeof item === "object" && item !== null,
  );
  if (!first) return undefined;

  const message = firstString(first.msg);
  if (!message) return undefined;

  const location = Array.isArray(first.loc)
    ? first.loc
        .filter(
          (part): part is string | number =>
            typeof part === "string" || typeof part === "number",
        )
        .filter((part) => part !== "body")
        .join(".")
    : "";

  return location ? `${location}: ${message}` : message;
}

/** Return a short user-facing message from OpenRAG/FastAPI error responses. */
export function formatSettingsUpdateError(
  data: Record<string, unknown>,
): string {
  const error = firstString(data.error);
  if (error) return error;

  const detail = data.detail;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    const validationMessage = formatValidationDetail(detail);
    if (validationMessage) return validationMessage;
  }
  if (typeof detail === "object" && detail !== null) {
    const structuredDetail = detail as Record<string, unknown>;
    const detailMessage = firstString(
      structuredDetail.message,
      structuredDetail.error,
    );
    if (detailMessage) return detailMessage;
  }

  const message = firstString(data.message, data.errorMessage);
  if (message) return message;

  if (typeof data.error === "object" && data.error !== null) {
    const structuredError = data.error as Record<string, unknown>;
    const errorMessage = firstString(
      structuredError.message,
      structuredError.error,
    );
    if (errorMessage) return errorMessage;
  }

  return "Failed to update settings";
}
