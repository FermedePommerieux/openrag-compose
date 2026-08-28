import { FileText, GitBranch, Link2 } from "lucide-react";

import type { FunctionCall as FunctionCallType } from "../../_types/types";

type RelationPath = {
  from_filename?: string;
  from_document_id?: string;
  relation_role?: string;
};

type ToolResultItem = {
  text_key?: string;
  data?: { file_path?: string; text?: string };
  filename?: string;
  page?: number;
  score?: number;
  source_url?: string | null;
  text?: string;
  chunk_id?: string;
  id?: string;
  retrieval_plane?: "direct" | "context";
  retrieval_relation_depth?: number;
  retrieval_relation_paths?: RelationPath[];
  retrieval_relevance?: { level?: string; reason?: string };
};

interface FunctionCallResultProps {
  result: FunctionCallType["result"];
}

function ResultCard({ item }: { item: ToolResultItem }) {
  const displayFilename = item.data?.file_path || item.filename;
  const text = item.data?.text || item.text;
  const isContext = item.retrieval_plane === "context";
  const firstPath = item.retrieval_relation_paths?.[0];

  return (
    <div
      className={`fc-result rounded-md border p-2.5 ${
        isContext
          ? "border-amber-500/20 bg-amber-500/5"
          : "border-emerald-500/20 bg-emerald-500/5"
      }`}
    >
      <div className="flex items-start gap-2">
        {isContext ? (
          <GitBranch className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-500" />
        ) : (
          <FileText className="mt-0.5 h-3.5 w-3.5 shrink-0 text-emerald-500" />
        )}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="truncate font-medium text-foreground">
              {displayFilename || "Untitled document"}
            </span>
            <span
              className={`rounded-full px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide ${
                isContext
                  ? "bg-amber-500/10 text-amber-600 dark:text-amber-400"
                  : "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
              }`}
            >
              {isContext
                ? item.retrieval_relevance?.level || "contextual"
                : "direct"}
            </span>
            {typeof item.retrieval_relation_depth === "number" && (
              <span className="text-[10px] text-muted-foreground">
                {item.retrieval_relation_depth} hop
                {item.retrieval_relation_depth === 1 ? "" : "s"}
              </span>
            )}
          </div>

          {firstPath && isContext && (
            <div className="mt-1 flex items-center gap-1 text-[11px] text-muted-foreground">
              <Link2 className="h-3 w-3 shrink-0" />
              <span className="truncate">
                {firstPath.relation_role || "related"} from{" "}
                {firstPath.from_filename ||
                  firstPath.from_document_id ||
                  "anchor document"}
              </span>
            </div>
          )}

          <div className="mt-0.5">
            {typeof item.page === "number" && item.page > 0 && (
              <span className="mr-2 text-[11px] text-muted-foreground">
                Page {item.page}
              </span>
            )}
            {typeof item.score === "number" && Number.isFinite(item.score) && (
              <span className="text-[11px] text-muted-foreground">
                Score {item.score.toFixed(3)}
              </span>
            )}
          </div>

          {text && (
            <div className="mt-1.5 max-h-32 overflow-y-auto whitespace-pre-wrap text-xs text-foreground">
              {text.length > 300 ? `${text.substring(0, 300)}...` : text}
            </div>
          )}

          {item.retrieval_relevance?.reason && isContext && (
            <div className="mt-1 text-[11px] text-muted-foreground">
              {item.retrieval_relevance.reason}
            </div>
          )}

          {item.source_url && (
            <a
              href={item.source_url}
              target="_blank"
              rel="noopener noreferrer"
              className="mt-1 inline-flex items-center text-[11px] text-blue-500 hover:underline"
            >
              Open source
            </a>
          )}

          {item.text_key && (
            <div className="mt-1 text-[11px] text-muted-foreground">
              Key: {item.text_key}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export function FunctionCallResult({ result }: FunctionCallResultProps) {
  if (!Array.isArray(result)) {
    return (
      <div className="text-xs text-muted-foreground">
        <span className="font-medium">Result:</span>
        <pre className="mt-1 overflow-x-auto rounded bg-muted/30 p-2 text-xs">
          {JSON.stringify(result, null, 2)}
        </pre>
      </div>
    );
  }

  const isNestedFormat =
    result.length > 0 &&
    result[0]?.results &&
    Array.isArray(result[0].results) &&
    !result[0].text_key;
  const items = (
    isNestedFormat ? result[0].results : result
  ) as ToolResultItem[];
  const direct = items.filter((item) => item.retrieval_plane !== "context");
  const context = items.filter((item) => item.retrieval_plane === "context");

  return (
    <div className="text-xs text-muted-foreground">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <span className="font-medium text-foreground">Document retrieval</span>
        <span className="rounded-full bg-emerald-500/10 px-2 py-0.5 text-emerald-600 dark:text-emerald-400">
          {direct.length} direct
        </span>
        {context.length > 0 && (
          <span className="rounded-full bg-amber-500/10 px-2 py-0.5 text-amber-600 dark:text-amber-400">
            {context.length} relation context
          </span>
        )}
      </div>

      <div className="space-y-2">
        {direct.map((item, index) => (
          <ResultCard
            key={item.chunk_id || item.id || `direct-${index}`}
            item={item}
          />
        ))}
      </div>

      {context.length > 0 && (
        <details
          className="mt-2 rounded-md border border-amber-500/20 bg-amber-500/5 p-2"
          open
        >
          <summary className="cursor-pointer font-medium text-amber-700 dark:text-amber-300">
            Intentionally retained relation context ({context.length})
          </summary>
          <p className="my-2 text-[11px] text-muted-foreground">
            These documents are linked to direct matches. They remain visible
            for human review, but are not presented as direct evidence.
          </p>
          <div className="space-y-2">
            {context.map((item, index) => (
              <ResultCard
                key={item.chunk_id || item.id || `context-${index}`}
                item={item}
              />
            ))}
          </div>
        </details>
      )}

      <div className="mt-2 text-[11px] text-muted-foreground">
        Found {items.length} document result{items.length === 1 ? "" : "s"}
      </div>
    </div>
  );
}
