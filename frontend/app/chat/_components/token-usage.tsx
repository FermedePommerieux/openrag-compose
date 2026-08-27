import { Zap } from "lucide-react";
import type { TokenUsage as TokenUsageType } from "../_types/types";

interface TokenUsageProps {
  usage: TokenUsageType;
}

export function TokenUsage({ usage }: TokenUsageProps) {
  // Guard against partial/malformed usage data
  if (
    typeof usage.input_tokens !== "number" ||
    typeof usage.output_tokens !== "number"
  ) {
    return null;
  }

  return (
    <div className="flex items-center gap-2 mt-2 text-xs text-muted-foreground">
      <Zap className="h-3 w-3" />
      <span>
        {usage.input_tokens.toLocaleString()} in /{" "}
        {usage.output_tokens.toLocaleString()} out
        {usage.input_tokens_details?.cached_tokens ? (
          <span className="text-green-500 ml-1">
            ({usage.input_tokens_details.cached_tokens.toLocaleString()} cached)
          </span>
        ) : null}
        {usage.output_tokens_details?.reasoning_tokens ? (
          <span className="ml-1">
            ({usage.output_tokens_details.reasoning_tokens.toLocaleString()}{" "}
            reasoning)
          </span>
        ) : null}
        {typeof usage.cost_usd === "number" ? (
          <span className="ml-1 font-medium text-foreground">
            · ${usage.cost_usd.toFixed(4)}
          </span>
        ) : usage.cost_complete === false ? (
          <span className="ml-1">· cost unavailable for one model</span>
        ) : null}
      </span>
    </div>
  );
}
