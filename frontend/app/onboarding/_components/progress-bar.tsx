import { ArrowRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { trackButton } from "@/lib/analytics";

interface ProgressBarProps {
  currentStep: number;
  totalSteps: number;
  onSkip?: () => void;
}

export function ProgressBar({
  currentStep,
  totalSteps,
  onSkip,
}: ProgressBarProps) {
  const progressPercentage = ((currentStep + 1) / totalSteps) * 100;

  return (
    <div className="w-full flex items-center px-3 gap-2 sm:px-6 sm:gap-4">
      <div className="hidden flex-1 sm:block" />
      <div className="flex min-w-0 flex-1 items-center gap-2 sm:flex-none sm:gap-3">
        <div className="h-1 min-w-0 flex-1 bg-background dark:bg-muted rounded-full overflow-hidden sm:w-48">
          <div
            className="h-full transition-all duration-300 ease-in-out"
            data-testid={`progress-bar-${currentStep}`}
            style={{
              width: `${progressPercentage}%`,
              background: "linear-gradient(to right, #773EFF, #22A7AF)",
            }}
          />
        </div>
        <span className="text-xs text-muted-foreground whitespace-nowrap">
          {currentStep + 1}/{totalSteps}
        </span>
      </div>
      <div className="flex shrink-0 justify-end sm:flex-1">
        {currentStep > 1 && onSkip && (
          <Button
            variant="link"
            data-testid="skip-overview-button"
            size="sm"
            onClick={() => {
              trackButton({
                CTA: "Skip Overview",
                elementId: "skip-overview-button",
                namespace: "onboarding",
              });
              onSkip?.();
            }}
            className="flex items-center gap-1 px-2 text-mmd !text-placeholder-foreground hover:!text-foreground hover:!no-underline sm:gap-2 sm:px-3"
          >
            <span className="sm:hidden">Skip</span>
            <span className="hidden sm:inline">Skip overview</span>
            <ArrowRight className="w-4 h-4" />
          </Button>
        )}
      </div>
    </div>
  );
}
