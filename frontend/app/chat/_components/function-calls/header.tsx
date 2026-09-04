import { ChevronDown, ChevronRight, Settings } from "lucide-react";

interface FunctionCallsHeaderProps {
  uniqueToolCount: number;
  totalCalls: number;
  isExpanded: boolean;
  onToggle: () => void;
}

export function FunctionCallsHeader({
  uniqueToolCount,
  totalCalls,
  isExpanded,
  onToggle,
}: FunctionCallsHeaderProps) {
  return (
    <div
      className={`fc-card flex min-w-0 max-w-full items-center gap-2 overflow-hidden bg-function-call-header hover:bg-function-call-header-hover border border-blue-500/20 p-3 cursor-pointer transition-colors ${
        isExpanded ? "rounded-t-lg" : "rounded-lg"
      }`}
      onClick={onToggle}
    >
      <Settings className="h-4 w-4 shrink-0 text-blue-400" />
      <span className="min-w-0 flex-1 truncate text-sm font-medium text-blue-400">
        Used {uniqueToolCount} {uniqueToolCount === 1 ? "tool" : "tools"} (
        {totalCalls} calls)
      </span>
      {isExpanded ? (
        <ChevronDown className="h-4 w-4 shrink-0 text-blue-400" />
      ) : (
        <ChevronRight className="h-4 w-4 shrink-0 text-blue-400" />
      )}
    </div>
  );
}
