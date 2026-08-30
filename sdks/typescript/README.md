# OpenRAG TypeScript SDK

Official TypeScript/JavaScript SDK for the [OpenRAG](https://openr.ag) API.

## Installation

```bash
npm install openrag-sdk
# or
yarn add openrag-sdk
# or
pnpm add openrag-sdk
```

## Quick Start

```typescript
import { OpenRAGClient } from "openrag-sdk";

// Client auto-discovers OPENRAG_API_KEY and OPENRAG_URL from environment
const client = new OpenRAGClient();

// Simple chat
const response = await client.chat.create({ message: "What is RAG?" });
console.log(response.response);
console.log(`Chat ID: ${response.chatId}`);
```

## Configuration

The SDK can be configured via environment variables or constructor arguments:

| Environment Variable | Constructor Option | Description |
|---------------------|-------------------|-------------|
| `OPENRAG_API_KEY` | `apiKey` | API key for authentication (required) |
| `OPENRAG_URL` | `baseUrl` | Base URL for the OpenRAG frontend (default: `http://localhost:3000`) |

```typescript
// Using environment variables
const client = new OpenRAGClient();

// Using explicit arguments
const client = new OpenRAGClient({
  apiKey: "orag_...",
  baseUrl: "https://api.example.com",
});
```

## Chat

### Non-streaming

```typescript
const response = await client.chat.create({ message: "What is RAG?" });
console.log(response.response);
console.log(`Chat ID: ${response.chatId}`);

// Continue conversation
const followup = await client.chat.create({
  message: "Tell me more",
  chatId: response.chatId,
});
```

### Streaming with `create({ stream: true })`

Returns an async iterator directly:

```typescript
let chatId: string | null = null;
for await (const event of await client.chat.create({
  message: "Explain RAG",
  stream: true,
})) {
  if (event.type === "content") {
    process.stdout.write(event.delta);
  } else if (event.type === "sources") {
    for (const source of event.sources) {
      const pageInfo = source.page ? ` (page ${source.page})` : "";
      console.log(`\nSource: ${source.filename}${pageInfo}`);
    }
  } else if (event.type === "done") {
    chatId = event.chatId;
  }
}
```

### Streaming with `stream()`

Provides additional helpers for convenience:

```typescript
// Full event iteration
const stream = await client.chat.stream({ message: "Explain RAG" });
try {
  for await (const event of stream) {
    if (event.type === "content") {
      process.stdout.write(event.delta);
    }
  }

  // Access aggregated data after iteration
  console.log(`\nChat ID: ${stream.chatId}`);
  console.log(`Full text: ${stream.text}`);
  console.log(`Sources: ${stream.sources}`);
} finally {
  stream.close();
}

// Just text deltas
const stream = await client.chat.stream({ message: "Explain RAG" });
try {
  for await (const text of stream.textStream) {
    process.stdout.write(text);
  }
} finally {
  stream.close();
}

// Get final text directly
const stream = await client.chat.stream({ message: "Explain RAG" });
try {
  const text = await stream.finalText();
  console.log(text);
} finally {
  stream.close();
}
```

### Conversation History

```typescript
// List all conversations
const conversations = await client.chat.list();
for (const conv of conversations.conversations) {
  console.log(`${conv.chatId}: ${conv.title}`);
}

// Get specific conversation with messages
const conversation = await client.chat.get(chatId);
for (const msg of conversation.messages) {
  console.log(`${msg.role}: ${msg.content}`);
}

// Delete conversation
await client.chat.delete(chatId);
```

## Search

```typescript
// Basic search
const results = await client.search.query("document processing");
for (const result of results.results) {
  console.log(`${result.filename} (score: ${result.score})`);
  console.log(`  ${result.text.slice(0, 100)}...`);
}

// Search with filters
const results = await client.search.query("API documentation", {
  filters: {
    data_sources: ["api-docs.pdf"],
    document_types: ["application/pdf"],
  },
  limit: 5,
  scoreThreshold: 0.5,
});
```

### Verifiable exhaustive reading

Focused search discovers candidate documents. Exhaustive mode then reads every
indexed leaf chunk of one immutable document snapshot in source order. A document
is complete only when the returned coverage certificate says `complete === true`.

```typescript
// 1. Discover the relevant document.
const focused = await client.search.query("termination conditions");
const documentId = focused.results[0].document_id;
if (!documentId) throw new Error("Focused result has no document identity");

// 2. Read and verify every indexed chunk of that document snapshot.
let cursor: string | undefined;
let snapshot: string | null | undefined;
const allChunks = [];
let totalChunks = 0;
for (;;) {
  const page = await client.search.query("", {
    evidenceMode: "exhaustive",
    documentId,
    cursor,
    batchSize: 50,
  });
  if (!page.coverage) throw new Error("Missing coverage certificate");
  snapshot ??= page.coverage.snapshot_sha256;
  if (page.coverage.snapshot_sha256 !== snapshot) {
    throw new Error("Document snapshot changed during exhaustive reading");
  }
  allChunks.push(...page.results);
  totalChunks = page.coverage.total_chunks;
  if (page.coverage.complete) break;
  cursor = page.coverage.next_cursor ?? undefined;
  if (!cursor) throw new Error("Incomplete response has no continuation cursor");
}

if (allChunks.length !== totalChunks) throw new Error("Incomplete evidence set");
```

For a query-defined dossier, the backend can discover RRF seeds, close their
accessible PROV-O graph and run that verified loop for every linked document:

```typescript
const scope = await client.search.query(
  "all exchanges with the administration about Surface pastorale",
  { evidenceMode: "scope_exhaustive" },
);
console.log(
  scope.coverage?.complete,
  scope.coverage?.status_code,
  scope.coverage?.status_message,
);
// coverage_ratio is document-read progress, not independent proof of closure.
console.log(scope.documents?.length, scope.results.length);
```

An incomplete certificate exposes the graph/document limit or verification
failure; the response retains the leaf evidence, manifest and graph.

## Documents

```typescript
// Ingest a file (waits for completion by default)
const result = await client.documents.ingest({
  filePath: "./report.pdf",
});
console.log(`Status: ${result.status}`);
console.log(`Successful files: ${result.successful_files}`);

// Keep a link to the authoritative file for search/chat citations
const linkedResult = await client.documents.ingest({
  filePath: "./report.pdf",
  sourceUrl: "https://files.example.com/reports/report.pdf",
  archiveSource: false,
});

// Or retain the uploaded bytes in OpenRAG's authenticated local archive
// (available only on deployments without user authentication)
const archivedResult = await client.documents.ingest({
  filePath: "./report.pdf",
  archiveSource: true,
});

// Ingest without waiting (returns immediately with task_id)
const result = await client.documents.ingest({
  filePath: "./report.pdf",
  wait: false,
});
console.log(`Task ID: ${result.task_id}`);

// Poll for completion manually
const finalStatus = await client.documents.waitForTask(result.task_id);
console.log(`Status: ${finalStatus.status}`);
console.log(`Successful files: ${finalStatus.successful_files}`);

// Ingest from File object (browser)
const file = new File([...], "report.pdf");
const result = await client.documents.ingest({
  file,
  filename: "report.pdf",
});

// Delete a document
const result = await client.documents.delete("report.pdf");
console.log(`Success: ${result.success}`);
```

## Settings

```typescript
// Get settings
const settings = await client.settings.get();
console.log(`LLM Provider: ${settings.agent.llm_provider}`);
console.log(`LLM Model: ${settings.agent.llm_model}`);
console.log(`Embedding Model: ${settings.knowledge.embedding_model}`);

// Update settings
await client.settings.update({
  chunk_size: 1000,
  chunk_overlap: 200,
});
```

## Knowledge Filters

Knowledge filters are reusable, named filter configurations that can be applied to chat and search operations.

```typescript
// Create a knowledge filter
const result = await client.knowledgeFilters.create({
  name: "Technical Docs",
  description: "Filter for technical documentation",
  queryData: {
    query: "technical",
    filters: {
      document_types: ["application/pdf"],
    },
    limit: 10,
    scoreThreshold: 0.5,
  },
});
const filterId = result.id;

// Search for filters
const filters = await client.knowledgeFilters.search("Technical");
for (const filter of filters) {
  console.log(`${filter.name}: ${filter.description}`);
}

// Get a specific filter
const filter = await client.knowledgeFilters.get(filterId);

// Update a filter
await client.knowledgeFilters.update(filterId, {
  description: "Updated description",
});

// Delete a filter
await client.knowledgeFilters.delete(filterId);

// Use filter in chat
const response = await client.chat.create({
  message: "Explain the API",
  filterId,
});

// Use filter in search
const results = await client.search.query("API endpoints", { filterId });
```

## Error Handling

```typescript
import {
  OpenRAGError,
  AuthenticationError,
  NotFoundError,
  ValidationError,
  RateLimitError,
  ServerError,
} from "openrag-sdk";

try {
  const response = await client.chat.create({ message: "Hello" });
} catch (e) {
  if (e instanceof AuthenticationError) {
    console.log(`Invalid API key: ${e.message}`);
  } else if (e instanceof NotFoundError) {
    console.log(`Resource not found: ${e.message}`);
  } else if (e instanceof ValidationError) {
    console.log(`Invalid request: ${e.message}`);
  } else if (e instanceof RateLimitError) {
    console.log(`Rate limited: ${e.message}`);
  } else if (e instanceof ServerError) {
    console.log(`Server error: ${e.message}`);
  } else if (e instanceof OpenRAGError) {
    console.log(`API error: ${e.message} (status: ${e.statusCode})`);
  }
}
```

## Browser Support

This SDK works in both Node.js and browser environments. The main difference is file ingestion:

- **Node.js**: Use `filePath` option
- **Browser**: Use `file` option with a `File` or `Blob` object

## TypeScript

This SDK is written in TypeScript and provides full type definitions. All types are exported from the main module:

```typescript
import type {
  ChatResponse,
  StreamEvent,
  SearchResponse,
  IngestResponse,
  SettingsResponse,
} from "openrag-sdk";
```

## License

MIT
