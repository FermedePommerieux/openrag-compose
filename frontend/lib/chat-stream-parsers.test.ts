import assert from "node:assert/strict";
import { describe, it } from "node:test";

const parserModule = "./chat-stream-parsers.ts";
const { normalizeToolResult } = await import(parserModule);

describe("normalizeToolResult", () => {
  it("extracts provenance from a nested JSON ToolMessage artifact", () => {
    const source = {
      filename: "invoice.pdf",
      text: "<<<UNTRUSTED_DOC_CHUNK>>>\nPOMMERIEUX TEST\n<<<END_UNTRUSTED_DOC_CHUNK>>>",
      page: 1,
      document_id: "TEST_DOCUMENT_ID",
      chunk_id: "TEST_CHUNK_ID",
      source_url: "/api/source-files/TEST_DOCUMENT_ID.token",
    };
    let result: unknown = JSON.stringify({
      content: JSON.stringify([source]),
      artifact: [source],
    });
    result = JSON.stringify(JSON.stringify(result));

    assert.deepEqual(normalizeToolResult(result), [
      {
        ...source,
        text: "POMMERIEUX TEST",
      },
    ]);
  });

  it("does not evaluate a Python repr", () => {
    const repr = "{'artifact': [{'chunk_id': 'TEST_CHUNK_ID'}]}";

    assert.equal(normalizeToolResult(repr), repr);
  });

  it("cleans direct artifact arrays, including escaped transport fences", () => {
    const result = [
      {
        filename: "invoice.pdf",
        text: "\\<<<UNTRUSTED_DOC_CHUNK>>>\nPOMMERIEUX TEST\n\\<<<END_UNTRUSTED_DOC_CHUNK>>>",
      },
    ];

    assert.deepEqual(normalizeToolResult(result), [
      { filename: "invoice.pdf", text: "POMMERIEUX TEST" },
    ]);
  });
});
