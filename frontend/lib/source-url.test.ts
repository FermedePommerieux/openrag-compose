import assert from "node:assert/strict";
import { describe, it } from "node:test";

const sourceUrlModule = "./source-url.ts";
const { getDocumentPreviewSandbox, getPreviewSourceUrl, getSourcePreviewKind } =
  await import(sourceUrlModule);

describe("getDocumentPreviewSandbox", () => {
  it("allows Chromium's native PDF viewer to run", () => {
    assert.equal(getDocumentPreviewSandbox("report.pdf"), undefined);
    assert.equal(
      getDocumentPreviewSandbox(
        "report.bin",
        "application/pdf; charset=binary",
      ),
      undefined,
    );
  });

  it("keeps non-PDF document previews sandboxed", () => {
    assert.equal(
      getDocumentPreviewSandbox("notes.txt", "text/plain"),
      "allow-downloads",
    );
  });
});

describe("getPreviewSourceUrl", () => {
  it("targets a PDF reference page for managed sources", () => {
    assert.equal(
      getPreviewSourceUrl(
        "/api/source-files/abcdefghijklmnop.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        7,
      ),
      "/api/source-files/abcdefghijklmnop.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa?preview=true#page=7",
    );
  });

  it("preserves normal preview behavior without a valid page", () => {
    assert.equal(
      getPreviewSourceUrl("https://example.com/report.pdf", 0),
      "https://example.com/report.pdf",
    );
  });
});

describe("getSourcePreviewKind", () => {
  it("recognizes extensionless sources by MIME type", () => {
    assert.equal(getSourcePreviewKind("source", "application/pdf"), "document");
  });
});
