import assert from "node:assert/strict";
import { describe, it } from "node:test";

const citationsModule = "./markdown-citations.ts";
const { prepareChatMessageForClipboard, preprocessCitations } = await import(
  citationsModule
);

describe("preprocessCitations", () => {
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

  it("turns a chunk citation into an interactive source with provenance", () => {
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

  it("accepts Markdown code quoting around an exact Source identifier", () => {
    const result = preprocessCitations(
      "POMMERIEUX TEST (Source: `TEST_CHUNK_ID`)",
      [source],
    );

    assert.equal(result.text, "POMMERIEUX TEST [\\[1\\]](#citation-1)");
    assert.deepEqual(result.citedSources, [{ item: source, index: 1 }]);
  });

  it("links a bare code-formatted identifier only when it exactly matches an artifact", () => {
    const result = preprocessCitations(
      "Chunks: `TEST_CHUNK_ID` and `TEST_DOCUMENT_ID` and `unknown_chunk`.",
      [source],
    );

    assert.equal(
      result.text,
      "Chunks: [\\[1\\]](#citation-1) and `TEST_DOCUMENT_ID` and `unknown_chunk`.",
    );
    assert.deepEqual(result.citedSources, [{ item: source, index: 1 }]);
  });

  it("shows clickable retrieved sources when the model omits citation markers", () => {
    const duplicateChunk = {
      ...source,
      chunk_id: "TEST_CHUNK_ID_2",
      page: 2,
    };
    const secondSource = {
      ...source,
      filename: "second.pdf",
      chunk_id: "SECOND_CHUNK_ID",
      source_url: "/api/source-files/SECOND_DOCUMENT_ID.token",
    };

    const result = preprocessCitations("Answer without inline citations.", [
      source,
      duplicateChunk,
      secondSource,
    ]);

    assert.equal(result.text, "Answer without inline citations.");
    assert.deepEqual(result.citedSources, [
      { item: source, index: 1 },
      { item: secondSource, index: 2 },
    ]);
  });

  it("copies visible citation numbers without internal chunk identifiers", () => {
    const copied = prepareChatMessageForClipboard(
      "POMMERIEUX TEST (Source: TEST_CHUNK_ID)",
      [source],
    );

    assert.equal(copied, "POMMERIEUX TEST [1]");
    assert.equal(copied.includes("TEST_CHUNK_ID"), false);
    assert.equal(copied.includes("#citation-"), false);
  });
});
