"use client";

import { ArrowUpRight, Loader2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import {
  useGetIBMModelsQuery,
  useGetOllamaModelsQuery,
  useGetOpenAIModelsQuery,
} from "@/app/api/queries/useGetModelsQuery";
import { useGetSettingsQuery } from "@/app/api/queries/useGetSettingsQuery";
import { ConfirmationDialog } from "@/components/confirmation-dialog";
import { LabelWrapper } from "@/components/label-wrapper";
import { RequirePermission } from "@/components/require-permission";
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
import { Switch } from "@/components/ui/switch";
import { useAuth } from "@/contexts/auth-context";
import { useIsCloudBrand } from "@/contexts/brand-context";
import { trackButton } from "@/lib/analytics";
import { DEFAULT_KNOWLEDGE_SETTINGS } from "@/lib/constants";
import { resolveLangflowEditUrl } from "@/lib/url-utils";
import { cn } from "@/lib/utils";
import { useUpdateSettingsMutation } from "../../api/mutations/useUpdateSettingsMutation";
import { ModelSelector } from "../../onboarding/_components/model-selector";
import { getModelLogo } from "../_helpers/model-helpers";
import { LangflowIcon } from "./langflow-icon";

export function IngestSettingsSection() {
  const isCloudBrand = useIsCloudBrand();
  const { isAuthenticated, isNoAuthMode, isIbmAuthMode, runMode } = useAuth();

  const [isRestoringFlow, setIsRestoringFlow] = useState<boolean>(false);

  const [chunkSize, setChunkSize] = useState<number>(1024);
  const [chunkOverlap, setChunkOverlap] = useState<number>(50);
  const [chunkingStrategy, setChunkingStrategy] = useState<
    "character" | "hybrid"
  >("character");
  const [hybridMaxTokens, setHybridMaxTokens] = useState<number>(512);
  const [hybridMergePeers, setHybridMergePeers] = useState<boolean>(true);
  const [chunkValidationError, setChunkValidationError] = useState<
    string | null
  >(null);
  const [tableStructure, setTableStructure] = useState<boolean>(true);
  const [ocr, setOcr] = useState<boolean>(false);
  const [pictureDescriptions, setPictureDescriptions] =
    useState<boolean>(false);
  const [disableIngestWithLangflow, setDisableIngestWithLangflow] =
    useState<boolean>(false);

  const { data: settings = {} } = useGetSettingsQuery({
    enabled: isAuthenticated || isNoAuthMode,
  });

  const { data: openaiModels, isLoading: openaiLoading } =
    useGetOpenAIModelsQuery(
      { apiKey: "" },
      { enabled: settings?.providers?.openai?.configured === true },
    );
  const { data: ollamaModels, isLoading: ollamaLoading } =
    useGetOllamaModelsQuery(
      { endpoint: settings?.providers?.ollama?.endpoint },
      {
        enabled:
          settings?.providers?.ollama?.configured === true &&
          !!settings?.providers?.ollama?.endpoint,
      },
    );
  const { data: watsonxModels, isLoading: watsonxLoading } =
    useGetIBMModelsQuery(
      {
        endpoint: settings?.providers?.watsonx?.endpoint,
        apiKey: "",
        projectId: settings?.providers?.watsonx?.project_id,
      },
      {
        enabled:
          settings?.providers?.watsonx?.configured === true &&
          !!settings?.providers?.watsonx?.endpoint &&
          !!settings?.providers?.watsonx?.project_id,
      },
    );

  const groupedEmbeddingModels = useMemo(
    () =>
      [
        {
          group: "OpenAI",
          provider: "openai",
          icon: getModelLogo("", "openai"),
          models: openaiModels?.embedding_models || [],
          configured: settings.providers?.openai?.configured === true,
        },
        {
          group: "Ollama",
          provider: "ollama",
          icon: getModelLogo("", "ollama"),
          models: ollamaModels?.embedding_models || [],
          configured: settings.providers?.ollama?.configured === true,
        },
        {
          group: "IBM watsonx.ai",
          provider: "watsonx",
          icon: getModelLogo("", "watsonx"),
          models: watsonxModels?.embedding_models || [],
          configured: settings.providers?.watsonx?.configured === true,
        },
      ]
        .filter((p) => p.configured)
        .map((p) => ({
          group: p.group,
          icon: p.icon,
          options: p.models.map((m) => ({ ...m, provider: p.provider })),
        })),
    [
      openaiModels?.embedding_models,
      ollamaModels?.embedding_models,
      watsonxModels?.embedding_models,
      settings.providers?.openai?.configured,
      settings.providers?.ollama?.configured,
      settings.providers?.watsonx?.configured,
    ],
  );

  const isLoadingAnyEmbeddingModels =
    openaiLoading || ollamaLoading || watsonxLoading;

  const updateSettingsMutation = useUpdateSettingsMutation({
    onSuccess: () => {
      toast.success("Settings updated successfully");
    },
    onError: (error) => {
      toast.error("Failed to update settings", { description: error.message });
    },
  });

  const allEmbeddingOptions = useMemo(
    () => groupedEmbeddingModels.flatMap((g) => g.options),
    [groupedEmbeddingModels],
  );

  const handleEmbeddingModelChange = useCallback(
    (newModel: string, provider?: string) => {
      if (newModel && provider) {
        updateSettingsMutation.mutate({
          embedding_model: newModel,
          embedding_provider: provider,
        });
      } else if (newModel) {
        updateSettingsMutation.mutate({ embedding_model: newModel });
      }
    },
    [updateSettingsMutation],
  );

  const autoSelectedEmbedding = useRef(false);
  useEffect(() => {
    if (settings.knowledge?.embedding_model) {
      autoSelectedEmbedding.current = false;
      return;
    }
    if (autoSelectedEmbedding.current) return;
    if (allEmbeddingOptions.length > 0) {
      autoSelectedEmbedding.current = true;
      const fallback =
        allEmbeddingOptions.find((o) => o.default) || allEmbeddingOptions[0];
      handleEmbeddingModelChange(fallback.value, fallback.provider);
    }
  }, [
    settings.knowledge?.embedding_model,
    allEmbeddingOptions,
    handleEmbeddingModelChange,
  ]);

  useEffect(() => {
    const k = settings.knowledge;
    if (!k) return;
    if (k.chunk_size !== undefined) setChunkSize(k.chunk_size);
    if (k.chunk_overlap !== undefined) setChunkOverlap(k.chunk_overlap);
    if (k.chunking_strategy !== undefined)
      setChunkingStrategy(k.chunking_strategy);
    if (k.hybrid_max_tokens !== undefined)
      setHybridMaxTokens(k.hybrid_max_tokens);
    if (k.hybrid_merge_peers !== undefined)
      setHybridMergePeers(k.hybrid_merge_peers);
    if (k.table_structure !== undefined) setTableStructure(k.table_structure);
    if (k.ocr !== undefined) setOcr(k.ocr);
    if (k.picture_descriptions !== undefined)
      setPictureDescriptions(k.picture_descriptions);
    if (k.disable_ingest_with_langflow !== undefined)
      setDisableIngestWithLangflow(k.disable_ingest_with_langflow);
  }, [settings.knowledge]);

  const k = settings.knowledge;
  const knowledgeIngestDirty =
    chunkingStrategy !== (k?.chunking_strategy ?? "character") ||
    (chunkingStrategy === "character" &&
      (chunkSize !== (k?.chunk_size ?? chunkSize) ||
        chunkOverlap !== (k?.chunk_overlap ?? chunkOverlap))) ||
    (chunkingStrategy === "hybrid" &&
      (hybridMaxTokens !== (k?.hybrid_max_tokens ?? hybridMaxTokens) ||
        hybridMergePeers !== (k?.hybrid_merge_peers ?? hybridMergePeers))) ||
    tableStructure !== (k?.table_structure ?? tableStructure) ||
    ocr !== (k?.ocr ?? ocr) ||
    pictureDescriptions !== (k?.picture_descriptions ?? pictureDescriptions) ||
    disableIngestWithLangflow !==
      (k?.disable_ingest_with_langflow ?? disableIngestWithLangflow);

  const handleChunkSizeChange = (value: string) => {
    setChunkSize(Math.max(0, Number.parseInt(value, 10) || 0));
    setChunkValidationError(null);
  };

  const handleChunkOverlapChange = (value: string) => {
    setChunkOverlap(Math.max(0, Number.parseInt(value, 10) || 0));
    setChunkValidationError(null);
  };

  const handleKnowledgeIngestSave = () => {
    const chunkingPayload =
      chunkingStrategy === "character"
        ? {
            chunking_strategy: chunkingStrategy,
            chunk_size: chunkSize,
            chunk_overlap: chunkOverlap,
          }
        : {
            chunking_strategy: chunkingStrategy,
            hybrid_max_tokens: hybridMaxTokens,
            hybrid_merge_peers: hybridMergePeers,
          };
    trackButton({
      CTA: "Save Ingest Settings",
      elementId: "save-ingest-settings-button",
      namespace: "settings",
      payload: {
        ...chunkingPayload,
        table_structure: tableStructure,
        ocr,
        picture_descriptions: pictureDescriptions,
        disable_ingest_with_langflow: disableIngestWithLangflow,
      },
    });
    if (chunkingStrategy === "character" && chunkSize < 1) {
      const msg = "Chunk size must be at least 1";
      setChunkValidationError(msg);
      toast.error("Could not save ingest settings", { description: msg });
      return;
    }
    if (chunkingStrategy === "character" && chunkOverlap >= chunkSize) {
      const msg = "Chunk overlap must be less than chunk size";
      setChunkValidationError(msg);
      toast.error("Could not save ingest settings", { description: msg });
      return;
    }
    if (chunkingStrategy === "hybrid" && hybridMaxTokens < 1) {
      const msg = "Hybrid max tokens must be at least 1";
      setChunkValidationError(msg);
      toast.error("Could not save ingest settings", { description: msg });
      return;
    }
    updateSettingsMutation.mutate(
      {
        ...chunkingPayload,
        table_structure: tableStructure,
        ocr,
        picture_descriptions: pictureDescriptions,
        disable_ingest_with_langflow: disableIngestWithLangflow,
      },
      { onSuccess: () => setChunkValidationError(null) },
    );
  };

  const handleEditInLangflow = (closeDialog: () => void) => {
    trackButton({
      CTA: "Edit in Langflow - Ingest",
      elementId: "edit-langflow-ingest-button",
      namespace: "settings",
    });
    window.open(
      resolveLangflowEditUrl({
        flowId: settings.ingest_flow_id,
        editUrlOverride: settings.langflow_ingest_edit_url,
        publicUrl: settings.langflow_public_url,
        langflowPort: settings.langflow_port,
        isIbmAuthMode,
        runMode,
      }),
      "_blank",
      "noopener,noreferrer",
    );
    closeDialog();
  };

  const handleRestoreIngestFlow = (closeDialog: () => void) => {
    setIsRestoringFlow(true);

    trackButton({
      CTA: "Restore Flow - Ingest",
      elementId: "restore-ingest-flow-button",
      namespace: "settings",
    });
    fetch("/api/reset-flow/ingest", { method: "POST" })
      .then((res) =>
        res.text().then((text) => {
          const body = text ? JSON.parse(text) : {};
          if (!res.ok) {
            throw new Error(
              body.error ?? `HTTP ${res.status}: ${res.statusText}`,
            );
          }
        }),
      )
      .then(() => {
        setChunkSize(DEFAULT_KNOWLEDGE_SETTINGS.chunk_size);
        setChunkOverlap(DEFAULT_KNOWLEDGE_SETTINGS.chunk_overlap);
        setChunkingStrategy(DEFAULT_KNOWLEDGE_SETTINGS.chunking_strategy);
        setHybridMaxTokens(DEFAULT_KNOWLEDGE_SETTINGS.hybrid_max_tokens);
        setHybridMergePeers(DEFAULT_KNOWLEDGE_SETTINGS.hybrid_merge_peers);
        setTableStructure(DEFAULT_KNOWLEDGE_SETTINGS.table_structure);
        setOcr(DEFAULT_KNOWLEDGE_SETTINGS.ocr);
        setPictureDescriptions(DEFAULT_KNOWLEDGE_SETTINGS.picture_descriptions);
        setDisableIngestWithLangflow(false);
        setChunkValidationError(null);
        toast.success("Default ingest flow settings restored successfully");
        closeDialog();
      })
      .catch((err) => {
        console.error("Error restoring ingest flow:", err);
        toast.error(
          err.message || "Failed to restore default ingest flow settings",
        );
        closeDialog();
      })
      .finally(() => setIsRestoringFlow(false));
  };

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between mb-3">
          <CardTitle
            className={cn(
              "text-lg",
              isCloudBrand && "ibm-settings-section-title",
            )}
          >
            Knowledge Ingest
          </CardTitle>
          <RequirePermission perm="flows:edit">
            <div className="flex gap-2">
              <ConfirmationDialog
                trigger={
                  <Button ignoreTitleCase={true} variant="outline">
                    Restore flow
                  </Button>
                }
                title="Restore default Ingest flow"
                description="This restores defaults and discards all custom settings and overrides. This can't be undone."
                confirmText="Restore"
                variant="destructive"
                onConfirm={handleRestoreIngestFlow}
                isLoading={isRestoringFlow}
              />
              <ConfirmationDialog
                trigger={
                  <Button>
                    <LangflowIcon />
                    Edit in Langflow
                  </Button>
                }
                title="Edit Ingest flow in Langflow"
                description={
                  <>
                    <p className="mb-2">
                      You&apos;re entering Langflow. You can edit the{" "}
                      <b>Ingest flow</b> and other underlying flows. Manual
                      changes to components, wiring, or I/O can break this
                      experience.
                    </p>
                    <p className="mb-2">
                      To enable editing, you need to unlock the flow by clicking
                      on its name and disabling the <b>Lock flow</b> option.
                    </p>
                    <p>You can restore this flow from Settings.</p>
                  </>
                }
                confirmText="Proceed"
                confirmIcon={<ArrowUpRight />}
                variant="warning"
                onConfirm={handleEditInLangflow}
              />
            </div>
          </RequirePermission>
        </div>
        <CardDescription>
          Configure how files are ingested and stored for retrieval. The
          embedding model saves as soon as you pick one; chunk and ingest
          options use Save ingest settings. Edit in Langflow for full control.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="space-y-6">
          <div className="space-y-2">
            <LabelWrapper
              helperText="Saves immediately when you select a model"
              id="embedding-model-select"
              label="Embedding model"
              required={true}
            >
              <ModelSelector
                groupedOptions={groupedEmbeddingModels}
                noOptionsPlaceholder={
                  isLoadingAnyEmbeddingModels
                    ? "Loading models..."
                    : "No embedding models detected. Configure a provider first."
                }
                value={settings.knowledge?.embedding_model || ""}
                onValueChange={handleEmbeddingModelChange}
              />
            </LabelWrapper>
          </div>
          <section className="space-y-4" aria-labelledby="chunking-settings">
            <div>
              <h3 id="chunking-settings" className="font-medium">
                Chunking
              </h3>
              <p className="text-sm text-muted-foreground">
                Choose how new documents are divided before they are indexed.
              </p>
            </div>
            <div
              className="grid grid-cols-2 gap-3"
              role="radiogroup"
              aria-label="Chunking strategy"
            >
              <Button
                type="button"
                variant={
                  chunkingStrategy === "character" ? "default" : "outline"
                }
                onClick={() => setChunkingStrategy("character")}
                aria-pressed={chunkingStrategy === "character"}
              >
                Character
              </Button>
              <Button
                type="button"
                variant={chunkingStrategy === "hybrid" ? "default" : "outline"}
                onClick={() => setChunkingStrategy("hybrid")}
                aria-pressed={chunkingStrategy === "hybrid"}
              >
                Hybrid
              </Button>
            </div>
            {chunkingStrategy === "character" ? (
              <>
                <p className="text-sm text-muted-foreground">
                  Fixed-size chunking. Changing these values affects newly
                  indexed documents and normally requires reindexing existing
                  documents for a homogeneous corpus.
                </p>
                <div className="grid grid-cols-2 gap-4">
                  <LabelWrapper id="chunk-size" label="Chunk size">
                    <Input
                      id="chunk-size"
                      type="number"
                      min="1"
                      value={chunkSize}
                      onChange={(event) =>
                        handleChunkSizeChange(event.target.value)
                      }
                      className={
                        chunkValidationError ? "border-destructive" : ""
                      }
                    />
                  </LabelWrapper>
                  <LabelWrapper id="chunk-overlap" label="Chunk overlap">
                    <Input
                      id="chunk-overlap"
                      type="number"
                      min="0"
                      value={chunkOverlap}
                      onChange={(event) =>
                        handleChunkOverlapChange(event.target.value)
                      }
                      className={
                        chunkValidationError ? "border-destructive" : ""
                      }
                    />
                  </LabelWrapper>
                </div>
              </>
            ) : (
              <>
                <p className="text-sm text-muted-foreground">
                  Hybrid chunking uses Docling document structure to create
                  semantically coherent chunks. Explicit Hybrid mode fails if
                  HybridChunker is unavailable; OpenRAG does not silently fall
                  back to Character.
                </p>
                <div className="grid grid-cols-2 gap-4">
                  <LabelWrapper
                    id="hybrid-max-tokens"
                    label="Hybrid max tokens"
                  >
                    <Input
                      id="hybrid-max-tokens"
                      type="number"
                      min="1"
                      value={hybridMaxTokens}
                      onChange={(event) => {
                        setHybridMaxTokens(
                          Math.max(
                            0,
                            Number.parseInt(event.target.value, 10) || 0,
                          ),
                        );
                        setChunkValidationError(null);
                      }}
                      className={
                        chunkValidationError ? "border-destructive" : ""
                      }
                    />
                  </LabelWrapper>
                  <div className="flex items-center justify-between rounded-md border px-3 py-2">
                    <div>
                      <Label
                        htmlFor="hybrid-merge-peers"
                        className="cursor-pointer"
                      >
                        Merge peers
                      </Label>
                      <p className="text-sm text-muted-foreground">
                        Merge adjacent compatible chunks.
                      </p>
                    </div>
                    <Switch
                      id="hybrid-merge-peers"
                      checked={hybridMergePeers}
                      onCheckedChange={setHybridMergePeers}
                    />
                  </div>
                </div>
              </>
            )}
            {chunkValidationError ? (
              <p className="text-sm text-destructive" role="alert">
                {chunkValidationError}
              </p>
            ) : null}
          </section>
          <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-muted-foreground">
            <strong className="text-foreground">
              Index-affecting settings.
            </strong>{" "}
            Embedding, chunking, Docling extraction, OCR, tables, and picture
            descriptions change the indexed representation. Changes apply to
            future ingestion; existing indexed documents are not automatically
            rebuilt.
          </div>
          <div>
            <div className="flex items-center justify-between py-3 border-b border-border">
              <div className="flex-1">
                <Label
                  htmlFor="disable-ingest-with-langflow"
                  className="text-base font-medium cursor-pointer pb-3"
                >
                  Disable Langflow Ingestion
                </Label>
                <div className="text-sm text-muted-foreground">
                  Bypass Langflow for document ingestion and use traditional
                  processing.
                </div>
              </div>
              <Switch
                id="disable-ingest-with-langflow"
                checked={disableIngestWithLangflow}
                onCheckedChange={setDisableIngestWithLangflow}
              />
            </div>
            <div className="flex items-center justify-between py-3 border-b border-border">
              <div className="flex-1">
                <Label
                  htmlFor="table-structure"
                  className="text-base font-medium cursor-pointer pb-3"
                >
                  Table Structure
                </Label>
                <div className="text-sm text-muted-foreground">
                  Capture table structure during ingest.
                </div>
              </div>
              <Switch
                id="table-structure"
                checked={tableStructure}
                onCheckedChange={setTableStructure}
              />
            </div>
            <div className="flex items-center justify-between py-3 border-b border-border">
              <div className="flex-1">
                <Label
                  htmlFor="ocr"
                  className="text-base font-medium cursor-pointer pb-3"
                >
                  OCR
                </Label>
                <div className="text-sm text-muted-foreground">
                  Extracts text from images/PDFs. Ingest is slower when enabled.
                </div>
              </div>
              <Switch id="ocr" checked={ocr} onCheckedChange={setOcr} />
            </div>
            <div className="flex items-center justify-between py-3">
              <div className="flex-1">
                <Label
                  htmlFor="picture-descriptions"
                  className="text-base font-medium cursor-pointer pb-3"
                >
                  Picture Descriptions
                </Label>
                <div className="text-sm text-muted-foreground">
                  Adds captions for images. Ingest is slower when enabled.
                </div>
              </div>
              <Switch
                id="picture-descriptions"
                checked={pictureDescriptions}
                onCheckedChange={setPictureDescriptions}
              />
            </div>
          </div>
          <div className="flex justify-end pt-2">
            <Button
              onClick={handleKnowledgeIngestSave}
              disabled={
                updateSettingsMutation.isPending || !knowledgeIngestDirty
              }
              className="min-w-[120px]"
              size="sm"
              variant="outline"
            >
              {updateSettingsMutation.isPending ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Saving...
                </>
              ) : (
                "Save ingest settings"
              )}
            </Button>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
