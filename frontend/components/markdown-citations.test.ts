import assert from "node:assert/strict";
import { describe, it } from "node:test";

const citationsModule = "./markdown-citations.ts";
const { preprocessCitations } = await import(citationsModule);

describe("preprocessCitations", () => {
  it("turns a chunk citation into an interactive source with provenance", () => {
    const source = {
      filename: "invoice.pdf",
      text: "ÉMETTEUR: POMMERIEUX TEST",
      page: 1,
      document_id: "TEST_DOCUMENT_ID",
      chunk_id: "TEST_CHUNK_ID",
      source_url: "/api/source-files/TEST_DOCUMENT_ID.token",
      chunk_index: 2,
      chunking_strategy: "character",
    };

    const result = preprocessCitations(
      "POMMERIEUX TEST (Source: TEST_CHUNK_ID)",
      [source],
    );

    assert.equal(result.text, "POMMERIEUX TEST [\\[1\\]](#citation-1)");
    assert.equal(result.citedSources.length, 1);
    assert.deepEqual(result.citedSources[0]?.item, source);
    assert.equal(
      result.citedSources[0]?.item.source_url,
      "/api/source-files/TEST_DOCUMENT_ID.token",
    );
  });
});
