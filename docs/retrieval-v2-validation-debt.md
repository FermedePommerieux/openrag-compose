# Retrieval v2 validation-tooling debt

This note records validation noise that predates the Retrieval v2 changes and
must not be hidden by unrelated formatting work.

## Frontend

`frontend/npm run lint` completes without errors in the Retrieval v2 files.
The repository still reports a Biome schema/CLI mismatch (schema 2.3.5 versus
CLI 2.5.1), a deprecated `linter.domains.recommended` setting, and existing
warnings in chat, settings, build, and test utility files. Those require a
separate frontend lint migration; they are not changed by the retrieval
runtime.

## TypeScript SDK

The SDK's historical lint command cannot run until its ESLint dependency is
declared. Its npm audit findings should likewise be handled as a dedicated
dependency-maintenance task, not folded into Retrieval v2.
