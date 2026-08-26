"use client";

import { Loader2 } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { useUpdateSettingsMutation } from "@/app/api/mutations/useUpdateSettingsMutation";
import type { KnowledgeSettings } from "@/app/api/queries/useGetSettingsQuery";
import { useGetSettingsQuery } from "@/app/api/queries/useGetSettingsQuery";
import { LabelWrapper } from "@/components/label-wrapper";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

type RetrievalStrategy = "rrf" | "weighted";
type RetrievalMode = "hybrid" | "lexical" | "vector";

// These mirror KnowledgeConfig defaults and only apply when an older server
// returns a null field. A current GET /settings returns resolved values.
const FALLBACK_RETRIEVAL = {
  strategy: "rrf" as RetrievalStrategy,
  mode: "hybrid" as RetrievalMode,
  lexicalCandidates: 50,
  vectorCandidates: 50,
  rrfK: 60,
  maxChunksPerDocument: 3,
  adaptiveMaxChunksPerDocument: 20,
};

function effectiveValues(knowledge?: KnowledgeSettings) {
  return {
    strategy: knowledge?.retrieval_strategy ?? FALLBACK_RETRIEVAL.strategy,
    mode: knowledge?.retrieval_mode ?? FALLBACK_RETRIEVAL.mode,
    lexicalCandidates:
      knowledge?.retrieval_lexical_candidates ??
      FALLBACK_RETRIEVAL.lexicalCandidates,
    vectorCandidates:
      knowledge?.retrieval_vector_candidates ??
      FALLBACK_RETRIEVAL.vectorCandidates,
    rrfK: knowledge?.retrieval_rrf_k ?? FALLBACK_RETRIEVAL.rrfK,
    maxChunksPerDocument:
      knowledge?.retrieval_max_chunks_per_document ??
      FALLBACK_RETRIEVAL.maxChunksPerDocument,
    adaptiveMaxChunksPerDocument:
      knowledge?.retrieval_adaptive_max_chunks_per_document ??
      FALLBACK_RETRIEVAL.adaptiveMaxChunksPerDocument,
  };
}

function NumericField({
  id,
  label,
  help,
  value,
  max,
  onChange,
}: {
  id: string;
  label: string;
  help: string;
  value: number;
  max: number;
  onChange: (value: number) => void;
}) {
  return (
    <LabelWrapper id={id} label={label} helperText={help}>
      <Input
        id={id}
        type="number"
        min="1"
        max={max}
        value={value}
        onChange={(event) =>
          onChange(Number.parseInt(event.target.value, 10) || 0)
        }
      />
    </LabelWrapper>
  );
}

export function RetrievalSettingsSection() {
  const { data: settings = {} } = useGetSettingsQuery();
  const [strategy, setStrategy] = useState<RetrievalStrategy>(
    FALLBACK_RETRIEVAL.strategy,
  );
  const [mode, setMode] = useState<RetrievalMode>(FALLBACK_RETRIEVAL.mode);
  const [lexicalCandidates, setLexicalCandidates] = useState(
    FALLBACK_RETRIEVAL.lexicalCandidates,
  );
  const [vectorCandidates, setVectorCandidates] = useState(
    FALLBACK_RETRIEVAL.vectorCandidates,
  );
  const [rrfK, setRrfK] = useState(FALLBACK_RETRIEVAL.rrfK);
  const [maxChunksPerDocument, setMaxChunksPerDocument] = useState(
    FALLBACK_RETRIEVAL.maxChunksPerDocument,
  );
  const [adaptiveMaxChunksPerDocument, setAdaptiveMaxChunksPerDocument] =
    useState(FALLBACK_RETRIEVAL.adaptiveMaxChunksPerDocument);
  const [validationError, setValidationError] = useState<string | null>(null);

  const updateSettings = useUpdateSettingsMutation({
    onSuccess: () => toast.success("Retrieval settings updated successfully"),
    onError: (error) =>
      toast.error("Could not update retrieval settings", {
        description: error.message,
      }),
  });

  useEffect(() => {
    const values = effectiveValues(settings.knowledge);
    setStrategy(values.strategy);
    setMode(values.mode);
    setLexicalCandidates(values.lexicalCandidates);
    setVectorCandidates(values.vectorCandidates);
    setRrfK(values.rrfK);
    setMaxChunksPerDocument(values.maxChunksPerDocument);
    setAdaptiveMaxChunksPerDocument(values.adaptiveMaxChunksPerDocument);
  }, [settings.knowledge]);

  const saved = effectiveValues(settings.knowledge);
  const isDirty =
    strategy !== saved.strategy ||
    mode !== saved.mode ||
    (strategy === "rrf" &&
      (lexicalCandidates !== saved.lexicalCandidates ||
        vectorCandidates !== saved.vectorCandidates ||
        rrfK !== saved.rrfK ||
        maxChunksPerDocument !== saved.maxChunksPerDocument ||
        adaptiveMaxChunksPerDocument !== saved.adaptiveMaxChunksPerDocument));

  const save = () => {
    if (strategy === "rrf") {
      if (lexicalCandidates < 1 || lexicalCandidates > 500) {
        setValidationError("Lexical candidates must be between 1 and 500.");
        return;
      }
      if (vectorCandidates < 1 || vectorCandidates > 500) {
        setValidationError("Vector candidates must be between 1 and 500.");
        return;
      }
      if (rrfK < 1 || rrfK > 1000) {
        setValidationError("RRF k must be between 1 and 1000.");
        return;
      }
      if (maxChunksPerDocument < 1 || maxChunksPerDocument > 100) {
        setValidationError(
          "Base chunks per document must be between 1 and 100.",
        );
        return;
      }
      if (
        adaptiveMaxChunksPerDocument < maxChunksPerDocument ||
        adaptiveMaxChunksPerDocument > 100
      ) {
        setValidationError(
          "Adaptive maximum must be between the base quota and 100.",
        );
        return;
      }
    }

    setValidationError(null);
    updateSettings.mutate({
      retrieval_strategy: strategy,
      ...(strategy === "rrf"
        ? {
            retrieval_mode: mode,
            retrieval_lexical_candidates: lexicalCandidates,
            retrieval_vector_candidates: vectorCandidates,
            retrieval_rrf_k: rrfK,
            retrieval_max_chunks_per_document: maxChunksPerDocument,
            retrieval_adaptive_max_chunks_per_document:
              adaptiveMaxChunksPerDocument,
          }
        : {}),
    });
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-lg">Retrieval</CardTitle>
        <CardDescription>
          Controls how the existing index is searched. Changes apply
          immediately; no reindex is required.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-8">
        <section className="space-y-4" aria-labelledby="standard-retrieval">
          <div>
            <h3 id="standard-retrieval" className="font-medium">
              Standard retrieval
            </h3>
            <p className="text-sm text-muted-foreground">
              Search mode Hybrid means lexical + semantic retrieval. It is
              separate from the Hybrid chunking option in Knowledge Ingest.
            </p>
          </div>
          <div className="grid gap-4 md:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="retrieval-strategy">Retrieval strategy</Label>
              <div
                id="retrieval-strategy"
                className="rounded-md border bg-muted/30 px-3 py-2 text-sm"
              >
                {strategy === "rrf"
                  ? "Standard (RRF) — Recommended"
                  : "Legacy weighted retrieval"}
              </div>
            </div>
            {strategy === "rrf" ? (
              <div className="space-y-2">
                <Label htmlFor="retrieval-mode">Search mode</Label>
                <Select
                  value={mode}
                  onValueChange={(value) => setMode(value as RetrievalMode)}
                >
                  <SelectTrigger id="retrieval-mode">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="hybrid">
                      Hybrid (lexical + semantic)
                    </SelectItem>
                    <SelectItem value="lexical">Lexical only</SelectItem>
                    <SelectItem value="vector">Vector only</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            ) : (
              <div className="space-y-2">
                <Label>Search mode</Label>
                <p className="rounded-md border bg-muted/30 px-3 py-2 text-sm text-muted-foreground">
                  Available with Standard (RRF). Legacy weighted retrieval keeps
                  its existing compatibility behavior.
                </p>
              </div>
            )}
          </div>
        </section>

        {strategy === "rrf" ? (
          <>
            <section
              className="space-y-4"
              aria-labelledby="candidate-retrieval"
            >
              <div>
                <h3 id="candidate-retrieval" className="font-medium">
                  Candidate retrieval
                </h3>
                <p className="text-sm text-muted-foreground">
                  Candidates are gathered from each lane before they are fused.
                </p>
              </div>
              <div className="grid gap-4 md:grid-cols-2">
                <NumericField
                  id="retrieval-lexical-candidates"
                  label="Lexical candidates"
                  help="1–500 candidates collected before fusion."
                  value={lexicalCandidates}
                  max={500}
                  onChange={setLexicalCandidates}
                />
                <NumericField
                  id="retrieval-vector-candidates"
                  label="Vector candidates"
                  help="1–500 candidates collected before fusion."
                  value={vectorCandidates}
                  max={500}
                  onChange={setVectorCandidates}
                />
              </div>
            </section>
            <section className="space-y-4" aria-labelledby="fusion-diversity">
              <div>
                <h3 id="fusion-diversity" className="font-medium">
                  Fusion &amp; diversity
                </h3>
              </div>
              <div className="grid gap-4 md:grid-cols-2">
                <NumericField
                  id="retrieval-rrf-k"
                  label="RRF k"
                  help="1–1000. RRF fusion constant, not a final top-k."
                  value={rrfK}
                  max={1000}
                  onChange={setRrfK}
                />
                <NumericField
                  id="retrieval-max-chunks-per-document"
                  label="Base chunks per document"
                  help="Guaranteed diversity quota before adaptive evidence fill."
                  value={maxChunksPerDocument}
                  max={100}
                  onChange={setMaxChunksPerDocument}
                />
                <NumericField
                  id="retrieval-adaptive-max-chunks-per-document"
                  label="Adaptive maximum"
                  help="Upper bound for focused search only; exhaustive mode has no top-k cutoff."
                  value={adaptiveMaxChunksPerDocument}
                  max={100}
                  onChange={setAdaptiveMaxChunksPerDocument}
                />
              </div>
            </section>
          </>
        ) : null}

        <section
          className="space-y-3 border-t pt-6"
          aria-labelledby="advanced-compatibility"
        >
          <div>
            <h3 id="advanced-compatibility" className="font-medium">
              Advanced / Compatibility
            </h3>
            <p className="text-sm text-muted-foreground">
              Legacy weighted retrieval is kept for compatibility with existing
              configurations. Opening this page never changes it.
            </p>
          </div>
          <Select
            value={strategy}
            onValueChange={(value) => setStrategy(value as RetrievalStrategy)}
          >
            <SelectTrigger aria-label="Compatibility retrieval strategy">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="rrf">Standard (RRF) — Recommended</SelectItem>
              <SelectItem value="weighted">
                Legacy weighted retrieval
              </SelectItem>
            </SelectContent>
          </Select>
          <p className="text-sm text-muted-foreground">
            Reranker configuration is managed through the backend API and
            persisted configuration; it is not exposed in this interface.
            Retrieval debug is also not exposed here.
          </p>
        </section>

        {validationError ? (
          <p className="text-sm text-destructive" role="alert">
            {validationError}
          </p>
        ) : null}
        <div className="flex justify-end">
          <Button
            onClick={save}
            disabled={!isDirty || updateSettings.isPending}
          >
            {updateSettings.isPending ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                Saving...
              </>
            ) : (
              "Save retrieval settings"
            )}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
