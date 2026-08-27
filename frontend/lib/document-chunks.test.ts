import assert from "node:assert/strict";
import { describe, it } from "node:test";

const documentChunksModule = "./document-chunks.ts";
const { fetchAllDocumentChunks, formatDocumentChunkScore } = await import(
  documentChunksModule
);

function response(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() {
      return body;
    },
  };
}

describe("fetchAllDocumentChunks", () => {
  it("follows every exhaustive cursor before returning chunks", async () => {
    const requests: Array<Record<string, unknown>> = [];
    const pages = [
      response({
        results: [{ chunk_index: 0, filename: "mail.eml", text: "first" }],
        coverage: {
          document_id: "document-1",
          complete: false,
          next_cursor: "cursor-2",
        },
      }),
      response({
        results: [{ chunk_index: 1, filename: "mail.eml", text: "second" }],
        coverage: {
          document_id: "document-1",
          complete: true,
          next_cursor: "",
        },
      }),
    ];

    const chunks = await fetchAllDocumentChunks({
      documentId: "document-1",
      filters: { owners: ["OpenArchiver"] },
      fetcher: async (_input: string, init?: RequestInit) => {
        requests.push(JSON.parse(String(init?.body)));
        return pages.shift()!;
      },
    });

    assert.deepEqual(
      chunks.map((chunk: { chunk_index?: number }) => chunk.chunk_index),
      [0, 1],
    );
    assert.equal(requests.length, 2);
    assert.equal(requests[0].evidenceMode, "exhaustive");
    assert.equal(requests[0].cursor, "");
    assert.equal(requests[1].cursor, "cursor-2");
    assert.deepEqual(requests[1].filters, { owners: ["OpenArchiver"] });
  });

  it("fails closed when incomplete coverage repeats a cursor", async () => {
    await assert.rejects(
      fetchAllDocumentChunks({
        documentId: "document-1",
        fetcher: async () =>
          response({
            results: [],
            coverage: {
              document_id: "document-1",
              complete: false,
              next_cursor: "same-cursor",
            },
          }),
      }),
      /repeated cursor/,
    );
  });
});

describe("formatDocumentChunkScore", () => {
  it("labels source-ordered exhaustive chunks without a relevance score", () => {
    assert.equal(formatDocumentChunkScore(null), "Source order");
    assert.equal(formatDocumentChunkScore(undefined), "Source order");
    assert.equal(formatDocumentChunkScore(1.234), "1.23 score");
  });
});
