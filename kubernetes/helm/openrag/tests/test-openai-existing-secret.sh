#!/bin/sh
set -eu

CHART_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
TEST_DIR=$(mktemp -d "${TMPDIR:-/tmp}/openrag-helm-openai.XXXXXX")
trap 'rm -rf "$TEST_DIR"' EXIT HUP INT TERM

render() {
  output=$1
  shift
  helm template openrag "$CHART_DIR" \
    --namespace openrag \
    --set-string global.opensearch.host=opensearch.example \
    --set llmProviders.openai.enabled=true \
    "$@" >"$output"
}

require_count() {
  expected=$1
  pattern=$2
  file=$3
  actual=$(grep -Ec "$pattern" "$file" || true)
  if [ "$actual" -ne "$expected" ]; then
    echo "expected $expected match(es) for '$pattern' in $file, got $actual" >&2
    exit 1
  fi
}

require_absent() {
  pattern=$1
  file=$2
  if grep -Eq "$pattern" "$file"; then
    echo "unexpected match for '$pattern' in $file" >&2
    exit 1
  fi
}

# Case A: an external Secret is referenced by both Deployments and never copied.
render "$TEST_DIR/external.yaml" \
  --set-string llmProviders.openai.apiKey= \
  --set-string llmProviders.openai.existingSecret=openrag-openai \
  --set-string llmProviders.openai.apiKeyKey=openai-api-key
require_count 2 '^[[:space:]]*- name: OPENAI_API_KEY$' "$TEST_DIR/external.yaml"
require_count 2 '^[[:space:]]*name: "openrag-openai"$' "$TEST_DIR/external.yaml"
require_count 2 '^[[:space:]]*key: "openai-api-key"$' "$TEST_DIR/external.yaml"
require_absent 'OPENAI_API_KEY=' "$TEST_DIR/external.yaml"
require_absent '^[[:space:]]*name: openrag-llm-providers$' "$TEST_DIR/external.yaml"

# Case B: the historical inline apiKey path remains available.
render "$TEST_DIR/inline.yaml" \
  --set-string llmProviders.openai.apiKey=TEST_VALUE_NOT_A_REAL_KEY \
  --set-string llmProviders.openai.existingSecret=
require_count 2 'OPENAI_API_KEY="TEST_VALUE_NOT_A_REAL_KEY"' "$TEST_DIR/inline.yaml"
require_count 1 'openai-api-key: "TEST_VALUE_NOT_A_REAL_KEY"' "$TEST_DIR/inline.yaml"
require_absent '^[[:space:]]*- name: OPENAI_API_KEY$' "$TEST_DIR/inline.yaml"

# Case C: rendering without either key source remains valid and compatible.
render "$TEST_DIR/empty.yaml" \
  --set-string llmProviders.openai.apiKey= \
  --set-string llmProviders.openai.existingSecret=
require_count 1 'OPENAI_API_KEY="None"' "$TEST_DIR/empty.yaml"
require_absent '^[[:space:]]*- name: OPENAI_API_KEY$' "$TEST_DIR/empty.yaml"

# Mixed providers: keep the Helm-managed provider Secret without copying OpenAI.
render "$TEST_DIR/mixed.yaml" \
  --set-string llmProviders.openai.apiKey=SHOULD_NOT_BE_RENDERED \
  --set-string llmProviders.openai.existingSecret=openrag-openai \
  --set llmProviders.anthropic.enabled=true \
  --set-string llmProviders.anthropic.apiKey=TEST_ANTHROPIC_NOT_A_REAL_KEY \
  --set llmProviders.watsonx.enabled=true \
  --set-string llmProviders.watsonx.apiKey=TEST_WATSONX_NOT_A_REAL_KEY
require_count 1 '^[[:space:]]*name: openrag-llm-providers$' "$TEST_DIR/mixed.yaml"
require_count 1 'anthropic-api-key: "TEST_ANTHROPIC_NOT_A_REAL_KEY"' "$TEST_DIR/mixed.yaml"
require_count 1 'watsonx-api-key: "TEST_WATSONX_NOT_A_REAL_KEY"' "$TEST_DIR/mixed.yaml"
require_absent 'openai-api-key:' "$TEST_DIR/mixed.yaml"
require_absent 'SHOULD_NOT_BE_RENDERED' "$TEST_DIR/mixed.yaml"

echo "OpenAI existingSecret Helm rendering tests passed"
